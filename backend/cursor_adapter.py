"""Cursor coding-agent adapter — @cursor/sdk streaming + session resume.

Uses ``Agent.create`` / ``Agent.resume`` + ``agent.send`` with ``onDelta`` and
``run.stream()``. Resume is this plugin's job (not UEFN-Ducky core). Core
only stores the upstream ``agentId`` and passes it back on the next turn.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from backend.agent.coding_agents.base import (
    CodingAgentCapabilities,
    CodingAgentInfo,
    CodingAgentLaunchResult,
    which_cli,
)
from backend.agent.coding_agents.cli_pty import run_cli_in_terminal
from backend.agent.coding_agents.mcp_inject import coding_agents_tmp_dir
from backend.agent.coding_agents.proc_exec import run_streaming_process
from backend.agent.coding_agents.settings_helpers import coding_agent_cfg
from .cursor_tool_unwrap import unwrap_cursor_tool

# #region agent log
def _dbg_tool_pairing(message: str, data: dict[str, Any]) -> None:
    try:
        with open(r"C:\Users\tas13\Documents\GitHub\UEFN-Ducky\debug-77e3f2.log", "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "sessionId": "77e3f2", "runId": "chat-lag-repro", "hypothesisId": "L-C",
                "location": "backend/agent/coding_agents/cursor.py",
                "message": message, "data": data, "timestamp": int(time.time() * 1000),
            }) + "\n")
    except Exception:
        pass
# #endregion

# @cursor/sdk contract: Agent.create/resume + send → stream events + RunResult.
# The runner ALWAYS emits NDJSON on stdout (one event per line) and forwards
# result.error — an opaque exit is useless.
_CURSOR_SDK_RUNNER = r"""
import fs from "node:fs";

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function parseArgsBlob(a) {
  if (a == null) return {};
  if (typeof a === "string") {
    try {
      const parsed = JSON.parse(a);
      return parsed && typeof parsed === "object" ? parsed : { raw: a };
    } catch {
      return { raw: a };
    }
  }
  return a && typeof a === "object" ? a : {};
}

/**
 * Cursor onDelta ToolCall is a discriminated union
 * `{ type: "read"|"edit"|"mcp"|…, args }`, not `{ name }`.
 * MCP nests the real tool as args.toolName + args.args.
 * Missing that made every card show the fallback label "tool".
 */
function unwrapMcpTool(name, args) {
  const n = String(name || "").trim() || "tool";
  const a = args && typeof args === "object" && !Array.isArray(args) ? args : {};
  const wrap =
    n === "CallMcpTool" || n === "call_mcp_tool" || n === "mcp" || n === "tool";
  if (!wrap) return { name: n, args: a };
  const inner = String(a.toolName || a.tool_name || "").trim();
  if (!inner) return { name: n, args: a };
  return { name: inner, args: parseArgsBlob(a.args ?? a.arguments) };
}

function toolCallNameRaw(tc) {
  if (!tc || typeof tc !== "object") return "tool";
  if (typeof tc.name === "string" && tc.name && tc.name !== "tool") return tc.name;
  if (typeof tc.toolName === "string" && tc.toolName) return tc.toolName;
  const t = typeof tc.type === "string" ? tc.type : "";
  if (t && t !== "tool_call" && t !== "toolCall") return t;
  if (typeof tc.tool === "string" && tc.tool) return tc.tool;
  if (tc.function && typeof tc.function.name === "string" && tc.function.name) {
    return tc.function.name;
  }
  return "tool";
}

function toolArgsRaw(tc) {
  if (!tc || typeof tc !== "object") return {};
  let a = tc.args ?? tc.arguments ?? tc.input;
  if (a == null && tc.function) a = tc.function.arguments;
  return parseArgsBlob(a);
}

function unwrapToolCall(tc) {
  return unwrapMcpTool(toolCallNameRaw(tc), toolArgsRaw(tc));
}

function toolName(tc) {
  return unwrapToolCall(tc).name;
}

function toolArgs(tc) {
  return unwrapToolCall(tc).args;
}

function toolResultText(tc) {
  if (!tc || typeof tc !== "object") return "";
  const r = tc.result ?? tc.output ?? tc.content ?? tc.response;
  if (r == null) return "";
  if (typeof r === "string") return r;
  try {
    return JSON.stringify(r);
  } catch {
    return String(r);
  }
}

async function loadSdk() {
  try {
    return await import("@cursor/sdk");
  } catch (e) {
    console.error("SDK_UNAVAILABLE:" + (e && e.message ? e.message : e));
    process.exit(2);
  }
}

async function main() {
  const cfg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  const sdk = await loadSdk();

  if (cfg.cmd === "models") {
    const models = await sdk.Cursor.models.list({ apiKey: cfg.apiKey });
    process.stdout.write(JSON.stringify(models || []));
    return;
  }

  const mcpServers =
    cfg.mcpServers && Object.keys(cfg.mcpServers).length > 0
      ? cfg.mcpServers
      : undefined;

  let agent = null;
  let resumed = false;
  if (cfg.agentId) {
    try {
      agent = await sdk.Agent.resume(cfg.agentId, {
        apiKey: cfg.apiKey,
        model: { id: cfg.model },
        mcpServers,
      });
      resumed = true;
    } catch (e) {
      emit({
        type: "status",
        text:
          "Cursor session expired — starting a fresh agent (" +
          String(e && e.message ? e.message : e) +
          ")",
      });
      agent = null;
    }
  }

  if (!agent) {
    agent = await sdk.Agent.create({
      apiKey: cfg.apiKey,
      model: { id: cfg.model },
      local: { cwd: cfg.cwd, settingSources: [] },
      mcpServers,
    });
  }

  const agentId = String(agent.agentId || "");
  emit({ type: "meta", agentId, resumed });

  const prompt = String(cfg.prompt || "");
  let assistantLen = 0;
  let thinkingLen = 0;
  const seenToolStarts = new Set();
  const seenToolDones = new Set();

  const sendOpts = {};
  if (mcpServers) sendOpts.mcpServers = mcpServers;
  sendOpts.onDelta = ({ update }) => {
    if (!update || typeof update !== "object") return;
    const utype = String(update.type || "");
    if (utype === "text-delta" && update.text) {
      const t = String(update.text);
      assistantLen += t.length;
      emit({ type: "text_delta", text: t });
    } else if (utype === "thinking-delta" && update.text) {
      const t = String(update.text);
      thinkingLen += t.length;
      emit({ type: "thinking", text: t });
    } else if (utype === "tool-call-started" || utype === "partial-tool-call") {
      const tc = update.toolCall;
      const callId = String(update.callId || "");
      if (utype === "tool-call-started" && callId) seenToolStarts.add(callId);
      emit({
        type: "tool_call",
        call_id: callId,
        name: toolName(tc),
        status: "running",
        args: toolArgs(tc),
      });
    } else if (utype === "tool-call-completed") {
      const tc = update.toolCall;
      const callId = String(update.callId || "");
      if (callId) seenToolDones.add(callId);
      const failed =
        tc &&
        typeof tc === "object" &&
        (tc.status === "error" ||
          tc.error ||
          tc.failed ||
          (tc.result && typeof tc.result === "object" && tc.result.status === "error"));
      emit({
        type: "tool_call",
        call_id: callId,
        name: toolName(tc),
        status: failed ? "error" : "completed",
        args: toolArgs(tc),
        result: toolResultText(tc),
      });
    }
  };

  const run = await agent.send(prompt, sendOpts);

  // Drain stream: status/usage always; content only as fallback for gaps
  // (length counters already advanced by onDelta prevent duplicates).
  for await (const event of run.stream()) {
    if (!event || typeof event !== "object") continue;
    const etype = String(event.type || "");
    if (etype === "status") {
      const text = String(event.message || event.status || "");
      if (text) emit({ type: "status", text });
      continue;
    }
    if (etype === "task") {
      const text = String(event.text || event.status || "");
      if (text) emit({ type: "status", text });
      continue;
    }
    if (etype === "usage" && event.usage) {
      emit({ type: "usage", usage: event.usage });
      continue;
    }
    if (etype === "thinking") {
      const text = String(event.text || "");
      if (text.length > thinkingLen) {
        emit({ type: "thinking", text: text.slice(thinkingLen) });
        thinkingLen = text.length;
      }
    } else if (etype === "assistant") {
      const parts = [];
      for (const block of (event.message && event.message.content) || []) {
        if (block && block.type === "text" && block.text) parts.push(String(block.text));
      }
      const full = parts.join("");
      if (full.length > assistantLen) {
        emit({ type: "text_delta", text: full.slice(assistantLen) });
        assistantLen = full.length;
      }
    } else if (etype === "tool_call") {
      const callId = String(event.call_id || event.callId || "");
      const status = String(event.status || "");
      const rawArgs =
        event.args && typeof event.args === "object"
          ? event.args
          : toolArgsRaw(event);
      const unwrapped = unwrapMcpTool(String(event.name || "tool"), rawArgs);
      if (status === "running" || status === "pending") {
        if (callId && seenToolStarts.has(callId)) continue;
        if (callId) seenToolStarts.add(callId);
        emit({
          type: "tool_call",
          call_id: callId,
          name: unwrapped.name,
          status: "running",
          args: unwrapped.args,
        });
      } else if (status === "completed" || status === "error") {
        if (callId && seenToolDones.has(callId)) continue;
        if (callId) seenToolDones.add(callId);
        emit({
          type: "tool_call",
          call_id: callId,
          name: unwrapped.name,
          status: status === "error" ? "error" : "completed",
          args: unwrapped.args,
          result:
            typeof event.result === "string"
              ? event.result
              : toolResultText({ result: event.result }),
        });
      }
    }
  }

  const result = await run.wait();
  const status = String((result && result.status) || "unknown");
  const text = String((result && result.result) || "");
  const err = result && result.error;
  const usage = (result && result.usage) || null;

  if (text.trim() && assistantLen === 0) {
    emit({ type: "text_delta", text: text });
  }

  emit({
    type: "done",
    status,
    result: text,
    agentId,
    usage,
    error: err && err.message ? { message: err.message, code: err.code || "" } : null,
  });

  try {
    if (typeof agent[Symbol.asyncDispose] === "function") {
      await agent[Symbol.asyncDispose]();
    } else if (typeof agent.close === "function") {
      await agent.close();
    } else if (typeof agent.dispose === "function") {
      await agent.dispose();
    }
  } catch (_) {
    /* ignore dispose errors */
  }

  if (status === "finished") {
    process.exit(0);
  }
  if (err && err.message) {
    const code = err.code ? "[" + err.code + "] " : "";
    console.error("CURSOR_RUN_FAILED: " + code + err.message);
  } else {
    console.error(
      "CURSOR_RUN_FAILED: run ended with status=" + status + " and no error detail",
    );
  }
  process.exit(1);
}

main().catch((e) => {
  const name = e && e.name && e.name !== "Error" ? e.name + ": " : "";
  console.error("CURSOR_RUN_FAILED: " + name + String(e && e.message ? e.message : e));
  process.exit(1);
});
"""

_TOOL_RESULT_MAX_CHARS = 4000


def _strip_node_noise(text: str) -> str:
    lines = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        if "ExperimentalWarning" in s or "DeprecationWarning" in s:
            continue
        if "SQLite is an experimental feature" in s:
            continue
        if "--trace-warnings" in s:
            continue
        lines.append(ln)
    return "\n".join(lines).strip()


def _tool_result_to_text(content: Any) -> str:
    if isinstance(content, str):
        text = content
    elif content is None:
        text = ""
    else:
        try:
            text = json.dumps(content, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(content)
    text = text.strip()
    if len(text) > _TOOL_RESULT_MAX_CHARS:
        text = text[:_TOOL_RESULT_MAX_CHARS] + "…(truncated)"
    return text


class _CursorStreamState:
    """Accumulates Cursor SDK NDJSON events into panel pushes + persisted blocks."""

    def __init__(self, conv_id: str, run_id: str, push: Callable[[dict[str, Any]], None]) -> None:
        self.conv_id = conv_id
        self.run_id = run_id
        self.push = push
        self.agent_id = ""
        self.streamed_text: list[str] = []
        self.final_text = ""
        self.is_error = False
        self.error_text = ""
        self.saw_json = False
        self.usage: dict[str, Any] = {}
        # Last per-step window from intermediate usage events; done.usage may be
        # cumulative and must not inflate "current context".
        self._last_step_context_tokens = 0
        self.blocks: list[dict[str, Any]] = []
        self._seg_text: list[str] = []
        self._seg_thinking: list[str] = []
        self._tools: dict[str, dict[str, Any]] = {}
        self._shown_tool: str | None = None
        self._tool_queue: list[str] = []
        self._delta_buf: list[str] = []
        self._delta_kind: str = "text_delta"
        self._delta_last_flush = 0.0
        self._done_status = ""

    def stdout_empty(self) -> bool:
        return not self.saw_json

    # Preserve every delta, but cross the Python→pywebview→React boundary at a
    # human-visible cadence instead of token speed. Tool boundaries and run end
    # still force an immediate flush, so ordering remains exact.
    _FLUSH_CHARS = 1200
    _FLUSH_SECS = 0.20

    def _queue_delta(self, kind: str, text: str) -> None:
        if kind != self._delta_kind:
            self.flush_stream()
            self._delta_kind = kind
        self._delta_buf.append(text)
        now = time.monotonic()
        if (
            sum(len(t) for t in self._delta_buf) >= self._FLUSH_CHARS
            or now - self._delta_last_flush >= self._FLUSH_SECS
        ):
            self.flush_stream()

    def flush_stream(self) -> None:
        if not self._delta_buf:
            self._delta_last_flush = time.monotonic()
            return
        text = "".join(self._delta_buf)
        self._delta_buf = []
        self._delta_last_flush = time.monotonic()
        self._emit({"type": self._delta_kind, "text": text})

    def _flush_segments(self) -> None:
        thinking = "".join(self._seg_thinking).strip()
        if thinking:
            self.blocks.append({"type": "thinking", "text": thinking})
        self._seg_thinking = []
        text = "".join(self._seg_text).strip()
        if text:
            self.blocks.append({"type": "text", "text": text})
        self._seg_text = []

    def trailing_text(self) -> str:
        return "".join(self._seg_text).strip()

    def finalize_blocks(self) -> list[dict[str, Any]]:
        thinking = "".join(self._seg_thinking).strip()
        if thinking:
            self.blocks.append({"type": "thinking", "text": thinking})
            self._seg_thinking = []
        return self.blocks

    def _push_tool_start(self, tool_id: str) -> None:
        self.flush_stream()
        info = self._tools.get(tool_id) or {}
        name = str(info.get("name") or "tool")
        self._emit(
            {
                "type": "tool",
                "text": f"⚙ {name}",
                "tool": {"name": name, "arguments": info.get("arguments") or {}, "status": "pending"},
            }
        )
        self._shown_tool = tool_id

    def _push_tool_done(self, tool_id: str, *, failed: bool, result_text: str) -> None:
        self.flush_stream()
        info = self._tools.pop(tool_id, None) or {}
        name = str(info.get("name") or "tool")
        args = info.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        started = float(info.get("started_at") or 0.0)
        ms = int((time.monotonic() - started) * 1000) if started else 0
        status = "error" if failed else "success"
        tool_payload: dict[str, Any] = {
            "name": name,
            "arguments": args,
            "status": status,
            "durationMs": ms,
            "result": result_text,
            "hint": "",
        }
        if not failed:
            try:
                from frontend.ui_web.verse_editor.agent_sync import file_edit_meta_for_stream

                file_edit = file_edit_meta_for_stream(name, args, result_text)
                if file_edit:
                    tool_payload["fileEdit"] = file_edit
            except Exception:
                pass
        self.blocks.append(
            {
                "type": "tool_call",
                "id": tool_id,
                "name": name,
                "arguments": args,
                "started": float(info.get("started_wall") or 0.0),
                "duration_ms": ms,
                "result": {"ok": not failed, "data": result_text, "hint": ""},
                "status": status,
                **({"file_edit": tool_payload["fileEdit"]} if "fileEdit" in tool_payload else {}),
            }
        )
        self._emit(
            {
                "type": "tool_done",
                "text": f"⚙ {name} · {status}" + (f" · {ms}ms" if ms else ""),
                "success": not failed,
                "tool": tool_payload,
            }
        )

    def _resolve_tool(self, tool_id: str, *, failed: bool, result_text: str) -> None:
        if tool_id == self._shown_tool:
            self._push_tool_done(tool_id, failed=failed, result_text=result_text)
            self._shown_tool = None
            if self._tool_queue:
                self._push_tool_start(self._tool_queue.pop(0))
            return
        if tool_id in self._tool_queue:
            self._tool_queue.remove(tool_id)
            self._push_tool_start(tool_id)
            self._shown_tool = None
            self._push_tool_done(tool_id, failed=failed, result_text=result_text)
            if self._tool_queue and self._shown_tool is None:
                self._push_tool_start(self._tool_queue.pop(0))

    def finish_unresolved_tools(self, *, cancelled: bool) -> None:
        note = (
            "Cancelled before the tool finished."
            if cancelled
            else "Turn ended before the tool reported a result."
        )
        leftovers = ([self._shown_tool] if self._shown_tool else []) + list(self._tool_queue)
        # #region agent log
        if leftovers:
            now = time.monotonic()
            _dbg_tool_pairing("cursor turn ended with unresolved tools", {
                "convId": self.conv_id, "cancelled": cancelled, "leftoverCount": len(leftovers),
                "trackedCount": len(self._tools),
                "tools": [
                    {
                        "id": str(tool_id)[:80],
                        "name": str((self._tools.get(tool_id) or {}).get("name") or "tool"),
                        "ageMs": int((now - float((self._tools.get(tool_id) or {}).get("started_at") or now)) * 1000),
                    }
                    for tool_id in leftovers[:30] if tool_id is not None
                ],
            })
        # #endregion
        self._tool_queue = []
        for tool_id in leftovers:
            if tool_id is None or tool_id not in self._tools:
                continue
            if tool_id != self._shown_tool:
                self._push_tool_start(tool_id)
            self._push_tool_done(tool_id, failed=True, result_text=note)
            self._shown_tool = None

    def _emit(self, event: dict[str, Any]) -> None:
        event.setdefault("conv_id", self.conv_id)
        if self.run_id:
            event.setdefault("run_id", self.run_id)
        self.push(event)

    def on_line(self, line: str) -> None:
        if not line.startswith("{"):
            return
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        self.saw_json = True
        kind = str(data.get("type") or "")
        if kind == "meta":
            aid = str(data.get("agentId") or "")
            if aid:
                self.agent_id = aid
            if data.get("resumed"):
                self._emit({"type": "status", "text": "Resumed Cursor session…"})
            else:
                self._emit({"type": "status", "text": "Starting Cursor…"})
        elif kind == "status":
            text = str(data.get("text") or "").strip()
            if text:
                self._emit({"type": "status", "text": text})
        elif kind == "thinking":
            text = str(data.get("text") or "")
            if text:
                self._seg_thinking.append(text)
                self._queue_delta("thinking", text)
        elif kind == "text_delta":
            text = str(data.get("text") or "")
            if text:
                self.streamed_text.append(text)
                self._seg_text.append(text)
                self._queue_delta("text_delta", text)
        elif kind == "tool_call":
            self._on_tool_call(data)
        elif kind == "usage":
            usage = data.get("usage")
            if isinstance(usage, dict):
                self._ingest_step_usage(usage)
        elif kind == "done":
            self._on_done(data)

    def _on_tool_call(self, data: dict[str, Any]) -> None:
        tid = str(data.get("call_id") or "") or f"tool-{len(self._tools)}"
        status = str(data.get("status") or "")
        name = str(data.get("name") or "tool")
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        name, args = unwrap_cursor_tool(name, args)
        # #region agent log
        if name == "tool":
            _dbg_tool_pairing("cursor emitted generic tool metadata", {
                "convId": self.conv_id, "toolId": tid[:80], "status": status,
                "eventKeys": sorted(str(k) for k in data.keys()),
                "hasResult": data.get("result") is not None,
            })
        # #endregion
        if status in ("running", "pending"):
            self._flush_segments()
            if tid in self._tools:
                # Cursor often fills old_string/new_string after the first running event.
                prev = self._tools[tid]
                if args:
                    old_args = prev.get("arguments") if isinstance(prev.get("arguments"), dict) else {}
                    prev["arguments"] = {**old_args, **args}
                if name and name not in ("tool", "mcp"):
                    prev["name"] = name
                return
            self._tools[tid] = {
                "name": name,
                "arguments": args,
                "started_at": time.monotonic(),
                "started_wall": time.time(),
            }
            try:
                from frontend.ui_web.verse_editor.agent_sync import seed_before_edit

                seed_before_edit(name, args)
            except Exception:
                pass
            if self._shown_tool is None:
                self._push_tool_start(tid)
            else:
                self._tool_queue.append(tid)
            return
        if status in ("completed", "error", "success"):
            if tid not in self._tools:
                self._flush_segments()
                self._tools[tid] = {
                    "name": name,
                    "arguments": args,
                    "started_at": time.monotonic(),
                    "started_wall": time.time(),
                }
                try:
                    from frontend.ui_web.verse_editor.agent_sync import seed_before_edit

                    seed_before_edit(name, args)
                except Exception:
                    pass
                if self._shown_tool is None:
                    self._push_tool_start(tid)
                else:
                    self._tool_queue.append(tid)
            else:
                prev = self._tools[tid]
                if args:
                    old_args = prev.get("arguments") if isinstance(prev.get("arguments"), dict) else {}
                    prev["arguments"] = {**old_args, **args}
                if name and name not in ("tool", "mcp"):
                    prev["name"] = name
            self._resolve_tool(
                tid,
                failed=status == "error",
                result_text=_tool_result_to_text(data.get("result")),
            )

    @staticmethod
    def _parse_usage_parts(usage: dict[str, Any]) -> tuple[int, int, int, int]:
        # SDK TokenUsage may use camelCase or snake_case.
        inp = int(usage.get("input_tokens") or usage.get("inputTokens") or 0)
        out = int(usage.get("output_tokens") or usage.get("outputTokens") or 0)
        cache_read = int(usage.get("cache_read_tokens") or usage.get("cacheReadTokens") or 0)
        cache_write = int(usage.get("cache_write_tokens") or usage.get("cacheWriteTokens") or 0)
        return inp, out, cache_read, cache_write

    def _ingest_step_usage(self, usage: dict[str, Any]) -> None:
        """Provisional per-step usage; done.usage overwrites billing if present."""
        self._ingest_usage(usage, prefer_step_window=False)
        window = int(self.usage.get("context_tokens") or 0)
        if window > 0:
            self._last_step_context_tokens = window

    def _ingest_usage(self, usage: dict[str, Any], *, prefer_step_window: bool = False) -> None:
        inp, out, cache_read, cache_write = self._parse_usage_parts(usage)
        if prefer_step_window and self._last_step_context_tokens > 0:
            context_tokens = self._last_step_context_tokens
        elif prefer_step_window:
            from frontend.ui_web.token_usage import estimate_context_window_tokens

            context_tokens = estimate_context_window_tokens(
                inp,
                cache_read,
                cache_write,
                num_turns=int(usage.get("num_turns") or usage.get("numTurns") or 0),
            )
        else:
            context_tokens = inp + cache_read + cache_write
        payload: dict[str, Any] = {
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "context_tokens": context_tokens,
            "cost_usd": usage.get("cost_usd") if isinstance(usage.get("cost_usd"), (int, float)) else None,
            "num_turns": int(usage.get("num_turns") or usage.get("numTurns") or 0),
            "model": str(usage.get("model") or ""),
        }
        limit = int(usage.get("context_limit") or usage.get("contextWindow") or 0)
        if limit > 0:
            payload["context_limit"] = limit
        self.usage = payload

    def _on_done(self, data: dict[str, Any]) -> None:
        self.flush_stream()
        aid = str(data.get("agentId") or "")
        if aid:
            self.agent_id = aid
        self._done_status = str(data.get("status") or "")
        text = data.get("result")
        if isinstance(text, str) and text.strip():
            self.final_text = text.strip()
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            self.is_error = True
            self.error_text = str(err.get("message") or "")
        elif self._done_status and self._done_status not in ("finished", "done", "success"):
            self.is_error = True
            self.error_text = self.error_text or f"Cursor run ended with status={self._done_status}"
        usage = data.get("usage")
        if isinstance(usage, dict):
            # Billing from done.usage; context window from last step when present.
            self._ingest_usage(usage, prefer_step_window=True)
        elif self._last_step_context_tokens > 0 and self.usage:
            self.usage["context_tokens"] = self._last_step_context_tokens


def _cursor_sdk_sandbox() -> Path | None:
    """Cached AppData folder with @cursor/sdk installed for the Node runner."""
    from frontend.settings import default_app_data_dir

    npm = which_cli("npm") or which_cli("npm.cmd")
    if not npm:
        return None
    root = default_app_data_dir() / "coding_agents" / "cursor_sdk"
    root.mkdir(parents=True, exist_ok=True)
    runner = root / "runner.mjs"
    pkg = root / "package.json"
    stamp = root / ".installed"
    pkg_body = json.dumps({"type": "module", "dependencies": {"@cursor/sdk": "latest"}}, indent=2)
    runner_body = _CURSOR_SDK_RUNNER.strip() + "\n"
    needs_install = (
        not stamp.is_file()
        or not (root / "node_modules" / "@cursor" / "sdk").is_dir()
        or pkg.read_text(encoding="utf-8") != pkg_body
    )
    pkg.write_text(pkg_body, encoding="utf-8")
    if not runner.is_file() or runner.read_text(encoding="utf-8") != runner_body:
        runner.write_text(runner_body, encoding="utf-8")
    if needs_install:
        try:
            proc = subprocess.run(
                [npm, "install", "--omit=dev", "--no-audit", "--no-fund"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        stamp.write_text("ok", encoding="utf-8")
    return root


def _run_sdk_runner(cfg: dict[str, Any], *, api_key: str, timeout_s: float) -> subprocess.CompletedProcess | None:
    """Run runner.mjs with a temp cfg file; None when node/sandbox missing.

    Used for the non-streaming ``models`` command only.
    """
    node = which_cli("node") or which_cli("node.exe")
    if not node:
        return None
    sandbox = _cursor_sdk_sandbox()
    if sandbox is None:
        return None
    cfg_path: Path | None = None
    try:
        fd, cfg_name = tempfile.mkstemp(prefix="cursor-sdk-", suffix=".json", dir=str(coding_agents_tmp_dir()))
        cfg_path = Path(cfg_name)
        with open(fd, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle)
        run_kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "cwd": str(sandbox),
            "timeout": timeout_s,
            "env": {
                **os.environ,
                "CURSOR_API_KEY": api_key,
                "NODE_NO_WARNINGS": "1",
            },
        }
        if os.name == "nt":
            run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.run(
            [node, str(sandbox / "runner.mjs"), str(cfg_path)],
            **run_kwargs,
        )
    finally:
        if cfg_path is not None:
            try:
                cfg_path.unlink(missing_ok=True)
            except OSError:
                pass


# Last-resort list when the live fetch has never succeeded. Only ids known
# valid from the SDK docs — the real list always comes from Cursor.models.list.
_FALLBACK_MODELS: list[dict[str, Any]] = [
    {"id": "auto", "name": "Auto", "provider": "Cursor"},
    {"id": "composer-2.5", "name": "composer-2.5", "provider": "Cursor"},
]

_MODELS_TTL_S = 6 * 60 * 60
_models_refresh_lock = threading.Lock()
_models_refresh_inflight = False


def _models_cache_path() -> Path:
    from frontend.settings import default_app_data_dir

    return default_app_data_dir() / "coding_agents" / "cursor_models.json"


def _read_models_cache(*, allow_stale: bool = False) -> list[dict[str, Any]] | None:
    try:
        data = json.loads(_models_cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not allow_stale and time.time() - float(data.get("fetched_at") or 0) > _MODELS_TTL_S:
        return None
    models = data.get("models")
    if not isinstance(models, list):
        return None
    rows = []
    for model in models:
        if not isinstance(model, dict) or not str(model.get("id") or "").strip():
            continue
        row = dict(model)
        # Older/current catalog payloads may label the Auto row as `default`,
        # while the SDK invocation contract uses `{ id: "auto" }`.
        if str(row.get("id") or "").strip().lower() == "default":
            row["id"] = "auto"
            row["name"] = str(row.get("name") or "Auto")
        rows.append(row)
    return rows or None


def _write_models_cache(models: list[dict[str, Any]]) -> None:
    path = _models_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"fetched_at": time.time(), "models": models}, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _fetch_models_via_sdk(api_key: str) -> list[dict[str, Any]] | None:
    if not api_key:
        return None
    try:
        proc = _run_sdk_runner({"cmd": "models", "apiKey": api_key}, api_key=api_key, timeout_s=120.0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc is None or proc.returncode != 0:
        return None
    try:
        raw = json.loads((proc.stdout or "").strip() or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, list):
        return None
    models = [
        {
            "id": "auto" if str(m.get("id") or "").strip().lower() == "default" else str(m.get("id")),
            # Keep the catalog label, while normalizing its legacy `default`
            # alias to the documented SDK invocation id above.
            "name": (
                "Auto"
                if str(m.get("id") or "").strip().lower() == "default"
                else str(m.get("displayName") or m.get("id"))
            ),
            "provider": "Cursor",
        }
        for m in raw
        if isinstance(m, dict) and str(m.get("id") or "").strip()
    ]
    return models or None


def _refresh_models_async(api_key: str) -> None:
    global _models_refresh_inflight
    with _models_refresh_lock:
        if _models_refresh_inflight:
            return
        _models_refresh_inflight = True

    def work() -> None:
        global _models_refresh_inflight
        try:
            models = _fetch_models_via_sdk(api_key)
            if models:
                _write_models_cache(models)
        finally:
            with _models_refresh_lock:
                _models_refresh_inflight = False

    threading.Thread(target=work, name="cursor-models-refresh", daemon=True).start()


def _known_models() -> list[dict[str, Any]]:
    models = _read_models_cache(allow_stale=True) or list(_FALLBACK_MODELS)
    if not any(str(row.get("id") or "").strip().lower() == "auto" for row in models):
        models = [{"id": "auto", "name": "Auto", "provider": "Cursor"}, *models]
    return models


class CursorAdapter:
    id = "cursor"
    label = "Cursor"
    capabilities = CodingAgentCapabilities(
        terminal_agent=True,
        chat_api=True,
        a2a=True,
        mcp_inject=True,
        needs_api_key=True,
        needs_cli=False,
        resume=True,
    )

    def detect(self, settings: Any) -> CodingAgentInfo:
        from backend.agent.secrets import get_key, has_key

        cfg = coding_agent_cfg(settings, self.id)
        enabled = bool(cfg.get("enabled", True))
        override = str(cfg.get("cli_path") or "")
        path = which_cli("cursor-agent", override) or which_cli("agent", override)
        default_args = str(cfg.get("default_args") or "")
        has_cursor_key = has_key("cursor")
        if not enabled:
            # Key/CLI may still be fine — picker hides disabled agents; say why.
            status = "Disabled — turn on to use in chat"
            available = False
        elif has_cursor_key:
            status = "API key saved"
            if path:
                status += f" · CLI: {path}"
            available = True
        elif path:
            status = f"CLI found (no API key): {path}"
            available = True
        else:
            status = (
                "Add a Cursor API key from cursor.com/dashboard/integrations — "
                "not your Cursor IDE login. Node.js is required. "
                "Optional fallback: install cursor-agent CLI."
            )
            available = False
        # Live model list from Cursor.models.list, cached on disk. A stale
        # cache still renders (real ids beat the fallback) while a background
        # refresh updates it for the next dropdown open.
        if has_cursor_key and enabled and _read_models_cache() is None:
            _refresh_models_async(get_key("cursor") or "")
        return CodingAgentInfo(
            id=self.id,
            label=self.label,
            enabled=enabled,
            available=available,
            status=status,
            cli_path=path or override,
            default_args=default_args,
            capabilities=self.capabilities,
            models=_known_models(),
        )

    def _try_node_sdk(
        self,
        *,
        prompt: str,
        cwd: str,
        model: str,
        mcp_config_path: str,
        api_key: str,
        conv_id: str,
        run_id: str,
        push: Any,
        session_id: str = "",
        cancel: threading.Event | None = None,
        timeout_s: float = 0.0,
    ) -> CodingAgentLaunchResult | None:
        """Run Cursor via @cursor/sdk with live NDJSON streaming into ``push``."""
        if not api_key:
            return None
        node = which_cli("node") or which_cli("node.exe")
        if not node:
            return None
        sandbox = _cursor_sdk_sandbox()
        if sandbox is None:
            return None

        mcp_servers: dict[str, Any] = {}
        if mcp_config_path:
            try:
                data = json.loads(Path(mcp_config_path).read_text(encoding="utf-8"))
                mcp_servers = data.get("mcpServers") or {}
            except (OSError, json.JSONDecodeError):
                mcp_servers = {}

        cfg = {
            "cmd": "prompt",
            "apiKey": api_key,
            "model": model,
            "cwd": cwd,
            "prompt": prompt,
            "mcpServers": mcp_servers,
            "agentId": (session_id or "").strip(),
        }
        cfg_path: Path | None = None
        try:
            fd, cfg_name = tempfile.mkstemp(
                prefix="cursor-sdk-", suffix=".json", dir=str(coding_agents_tmp_dir())
            )
            cfg_path = Path(cfg_name)
            with open(fd, "w", encoding="utf-8") as handle:
                json.dump(cfg, handle)

            state = _CursorStreamState(conv_id, run_id, push)
            push({"type": "status", "text": "Starting Cursor…", "conv_id": conv_id, "run_id": run_id})
            proc = run_streaming_process(
                argv=[node, str(sandbox / "runner.mjs"), str(cfg_path)],
                cwd=str(sandbox),
                env_extra={
                    "CURSOR_API_KEY": api_key,
                    "NODE_NO_WARNINGS": "1",
                },
                conv_id=conv_id,
                on_line=state.on_line,
                timeout_s=timeout_s,
                cancel=cancel,
            )
        except OSError as exc:
            return CodingAgentLaunchResult(
                ok=False,
                error=f"Cursor SDK launch failed: {exc}",
                status="error",
            )
        finally:
            if cfg_path is not None:
                try:
                    cfg_path.unlink(missing_ok=True)
                except OSError:
                    pass

        state.flush_stream()
        state.finish_unresolved_tools(cancelled=proc.cancelled)
        blocks = state.finalize_blocks()
        streamed = "".join(state.streamed_text).strip()
        reply = state.final_text or state.trailing_text() or ("" if blocks else streamed)
        new_session = state.agent_id or session_id
        err_tail = _strip_node_noise(proc.stderr_tail or "")

        if proc.returncode == 2 or "SDK_UNAVAILABLE" in (proc.stderr_tail or ""):
            return None

        if proc.cancelled:
            return CodingAgentLaunchResult(
                ok=False,
                upstream_session_id=new_session,
                reply_text=reply,
                streamed=bool(streamed) or bool(blocks),
                error="Cancelled",
                status="cancelled",
                blocks=blocks,
            )
        if proc.timed_out:
            return CodingAgentLaunchResult(
                ok=False,
                upstream_session_id=new_session,
                reply_text=reply,
                streamed=bool(streamed) or bool(blocks),
                error=f"Cursor SDK run timed out after {int(timeout_s)}s",
                status="timeout",
                blocks=blocks,
            )

        if state.is_error or (proc.returncode != 0 and not reply and not blocks):
            err = state.error_text or err_tail or proc.raw_tail or "Cursor SDK failed (no reply captured)"
            low = err.lower()
            if "authenticationerror" in low or "unauthorized" in low or "api key" in low:
                err += (
                    "\nCheck the Cursor API key in Settings → LLMs → Coding Agents "
                    "(create one at cursor.com/dashboard → Integrations)."
                )
            elif "out of usage" in low or "increase your limit" in low:
                err += (
                    "\nYour Cursor plan has no usage left for this model — pick "
                    "'Auto' in the model dropdown or raise limits at cursor.com/dashboard."
                )
            # Stale resume id — drop so the next turn creates a fresh agent.
            if session_id and (
                "not found" in low or "no such agent" in low or "expired" in low or "unknown agent" in low
            ):
                new_session = ""
            return CodingAgentLaunchResult(
                ok=False,
                upstream_session_id=new_session,
                reply_text=reply,
                streamed=bool(streamed) or bool(blocks),
                error=err,
                status="error",
                output_tail=err_tail,
                blocks=blocks,
            )

        return CodingAgentLaunchResult(
            ok=True,
            upstream_session_id=new_session,
            reply_text=reply,
            streamed=bool(streamed) or bool(blocks),
            output_tail=reply or err_tail,
            status="done",
            usage=state.usage,
            blocks=blocks,
        )

    def launch(
        self,
        *,
        prompt: str,
        system_prompt: str,
        cwd: str,
        conv_id: str,
        model: str,
        mcp_config_path: str,
        extra_args: str,
        cli_path: str,
        env: dict[str, str],
        push: Any,
        session_id: str = "",
        run_id: str = "",
        cancel: Any = None,
        timeout_s: float = 0.0,
        image_paths: list[str] | None = None,
    ) -> CodingAgentLaunchResult:
        from backend.agent.secrets import get_key

        api_key = get_key("cursor") or ""
        # With resume, the agent keeps memory — only prepend the bootstrap system
        # prompt on the first turn (no session yet). Follow-ups send the user text.
        full_prompt = prompt
        if system_prompt.strip() and not (session_id or "").strip():
            full_prompt = system_prompt.strip() + "\n\n" + prompt
        images = list(image_paths or [])
        if images:
            listed = "\n".join(f"- {p}" for p in images)
            full_prompt += (
                "\n\nThe user attached image file(s) with this message. Read these absolute "
                f"paths to view them:\n{listed}"
            )

        # A model must be selected explicitly. Cursor's advertised `default`
        # catalog row aliases the SDK's real `auto` invocation id.
        model_id = (model or "").strip()
        if not model_id:
            return CodingAgentLaunchResult(
                ok=False,
                error=(
                    "No Cursor model selected. Pick Auto or a concrete model "
                    "(e.g. Composer 2.5) for this chat or Ducky profile."
                ),
                status="error",
            )
        if model_id.lower() == "default":
            model_id = "auto"

        sdk = self._try_node_sdk(
            prompt=full_prompt,
            cwd=cwd,
            model=model_id,
            mcp_config_path=mcp_config_path,
            api_key=api_key,
            conv_id=conv_id,
            run_id=run_id,
            push=push,
            session_id=session_id,
            cancel=cancel,
            timeout_s=timeout_s,
        )
        if sdk is not None:
            return sdk

        # With an API key, stay on the SDK path — do not open a PowerShell
        # terminal fallback (that is what produced blank console windows).
        if api_key:
            return CodingAgentLaunchResult(
                ok=False,
                error=(
                    "Cursor SDK could not start: Node.js 22.13+ and npm must be on PATH "
                    "so the panel can install @cursor/sdk. Install Node, then retry."
                ),
                status="error",
            )

        binary = which_cli("cursor-agent", cli_path) or which_cli("agent", cli_path)
        if not binary:
            return CodingAgentLaunchResult(
                ok=False,
                error="Cursor SDK unavailable and cursor-agent CLI not found",
                status="error",
            )
        argv = [binary]
        extra = (extra_args or "").strip()
        if extra:
            import shlex

            argv.extend(shlex.split(extra, posix=False))
        # Never put multi-KB pastes on Windows argv (WinError 206).
        launch_prompt = full_prompt
        if len(full_prompt) > 2500:
            from backend.agent.coding_agents.mcp_inject import write_prompt_file

            prompt_file = write_prompt_file(full_prompt, conv_id=conv_id)
            launch_prompt = (
                "Open this UTF-8 file and follow every instruction in it exactly "
                f"(do not summarize first): {prompt_file}"
            )
        argv.append(launch_prompt)
        launch_env = dict(env)
        if api_key:
            launch_env["CURSOR_API_KEY"] = api_key
        return run_cli_in_terminal(
            argv=argv,
            cwd=cwd,
            conv_id=conv_id,
            title="Cursor Agent",
            env=launch_env,
            push=push,
            timeout_s=0.0,
            skip_approval=True,
        )
