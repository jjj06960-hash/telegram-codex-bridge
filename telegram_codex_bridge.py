#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = {
    "allowed_user_ids": [],
    "allowed_group_ids": [],
    "group_require_mention": False,
    "mention_patterns": ["@your_bot", "codex"],
    "codex_path": "codex",
    "codex_cwd": str(Path.home()),
    "codex_mode": "chat",
    "queue_path": "/tmp/claude_redis_queue.jsonl",
    "state_path": "/tmp/telegram_codex_bridge_state.json",
    "log_dir": str(Path.home() / "telegram-codex-bridge-logs/raw"),
    "attachment_dir": str(Path.home() / "telegram-codex-bridge-logs/raw/telegram/attachments"),
    "redis_result_queue": "claude_result_queue",
    "poll_timeout_seconds": 30,
    "max_message_chars": 3500,
    "send_ack": False,
    "typing_interval_seconds": 5,
    "user_channels": {},
    "system_context_files": [],
    "default_permission": "write",
    "require_approval_for_full": True,
    "full_access_keywords": [
        "computer use",
        "computer-use",
        "컴퓨터유즈",
        "컴퓨터 유즈",
        "화면 조작",
        "클릭",
        "앱 열어",
        "설치",
        "삭제",
        "휴지통",
        "권한",
        "전체권한",
        "full",
        "bypass",
        "ppt",
        "powerpoint",
        "파일변환",
        "파일 변환",
        "오디오",
        "음성",
        "whisper",
        "transcribe",
    ],
}


def load_dotenv(path):
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_config():
    config = dict(DEFAULT_CONFIG)
    custom = ROOT / "config.json"
    if custom.exists():
        config.update(json.loads(custom.read_text(encoding="utf-8")))
    return config


def channel_for_user(config, user_id):
    return config.get("user_channels", {}).get(str(user_id), "telegram")


def now_iso():
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def today():
    return dt.datetime.now().strftime("%Y-%m-%d")


def ensure_parent(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)


class TelegramAPI:
    def __init__(self, token):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"

    def call(self, method, payload=None):
        payload = payload or {}
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(f"{self.base}/{method}", data=data)
        with urllib.request.urlopen(req, timeout=90) as res:
            body = res.read().decode("utf-8")
        parsed = json.loads(body)
        if not parsed.get("ok"):
            raise RuntimeError(parsed)
        return parsed["result"]

    def get_updates(self, offset, timeout):
        payload = {"timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query"])}
        if offset:
            payload["offset"] = offset
        return self.call("getUpdates", payload)

    def send_message(self, chat_id, text, reply_to_message_id=None, max_chars=3500):
        chunks = split_text(text, max_chars)
        last = None
        for chunk in chunks:
            payload = {"chat_id": chat_id, "text": chunk}
            if reply_to_message_id:
                payload["reply_to_message_id"] = reply_to_message_id
            last = self.call("sendMessage", payload)
            time.sleep(0.2)
        return last

    def send_message_with_buttons(self, chat_id, text, buttons, reply_to_message_id=None, max_chars=3500):
        chunks = split_text(text, max_chars)
        last = None
        for index, chunk in enumerate(chunks):
            payload = {"chat_id": chat_id, "text": chunk}
            if reply_to_message_id:
                payload["reply_to_message_id"] = reply_to_message_id
            if index == len(chunks) - 1:
                payload["reply_markup"] = json.dumps({"inline_keyboard": buttons}, ensure_ascii=False)
            last = self.call("sendMessage", payload)
            time.sleep(0.2)
        return last

    def answer_callback_query(self, callback_query_id, text=None):
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return self.call("answerCallbackQuery", payload)

    def edit_message_text(self, chat_id, message_id, text, max_chars=3500):
        payload = {"chat_id": chat_id, "message_id": message_id, "text": split_text(text, max_chars)[0]}
        return self.call("editMessageText", payload)

    def send_chat_action(self, chat_id, action="typing"):
        return self.call("sendChatAction", {"chat_id": chat_id, "action": action})

    def get_file_path(self, file_id):
        return self.call("getFile", {"file_id": file_id})["file_path"]

    def download_file(self, file_path, dest):
        ensure_parent(dest)
        with urllib.request.urlopen(f"{self.file_base}/{file_path}", timeout=120) as res:
            Path(dest).write_bytes(res.read())


def split_text(text, max_chars):
    if len(text) <= max_chars:
        return [text]
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, max_chars)
        if cut < max_chars // 2:
            cut = max_chars
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return chunks


def load_state(config):
    path = Path(config["state_path"])
    if not path.exists():
        return {"offset": None, "mode": config["codex_mode"], "last_chat_id": None, "sessions": {}, "pending_approvals": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    state.setdefault("mode", config["codex_mode"])
    state.setdefault("sessions", {})
    state.setdefault("pending_approvals", {})
    return state


def save_state(config, state):
    path = Path(config["state_path"])
    ensure_parent(path)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def message_text(msg):
    return msg.get("text") or msg.get("caption") or ""


def sender_id(msg):
    user = msg.get("from") or {}
    return str(user.get("id", ""))


def chat_id(msg):
    return str((msg.get("chat") or {}).get("id", ""))


def sender_name(msg):
    user = msg.get("from") or {}
    parts = [user.get("first_name"), user.get("last_name")]
    name = " ".join([p for p in parts if p])
    return name or user.get("username") or sender_id(msg)


def is_allowed(config, msg):
    sid = sender_id(msg)
    cid = chat_id(msg)
    if sid not in set(map(str, config["allowed_user_ids"])):
        return False
    if cid.startswith("-") and cid not in set(map(str, config["allowed_group_ids"])):
        return False
    if cid.startswith("-") and config.get("group_require_mention"):
        text = message_text(msg)
        return any(pattern in text for pattern in config["mention_patterns"])
    return True


def has_attachment(msg):
    keys = ["document", "photo", "audio", "voice", "video"]
    return any(k in msg for k in keys)


def attachment_file_ids(msg):
    items = []
    if "document" in msg:
        doc = msg["document"]
        items.append(("document", doc["file_id"], doc.get("file_name") or "document"))
    if "voice" in msg:
        voice = msg["voice"]
        items.append(("voice", voice["file_id"], f"voice_{msg['message_id']}.oga"))
    if "audio" in msg:
        audio = msg["audio"]
        items.append(("audio", audio["file_id"], audio.get("file_name") or f"audio_{msg['message_id']}"))
    if "video" in msg:
        video = msg["video"]
        items.append(("video", video["file_id"], video.get("file_name") or f"video_{msg['message_id']}.mp4"))
    if "photo" in msg:
        photo = sorted(msg["photo"], key=lambda p: p.get("file_size", 0))[-1]
        items.append(("photo", photo["file_id"], f"photo_{msg['message_id']}.jpg"))
    return items


def safe_name(name):
    keep = []
    for char in name:
        if char.isalnum() or char in "._-":
            keep.append(char)
        else:
            keep.append("_")
    return "".join(keep).strip("_") or "attachment"


def log_markdown(config, channel, direction, msg_or_job, content, attachments=None):
    log_dir = Path(config["log_dir"]) / channel
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{today()}.md"
    attachments = attachments or []
    header = f"\n\n## {now_iso()} {direction}\n"
    meta = ""
    if isinstance(msg_or_job, dict):
        meta = f"- source: telegram\n- sender: {msg_or_job.get('sender_name', '')}\n- user_id: {msg_or_job.get('from_user_id', '')}\n- chat_id: {msg_or_job.get('chat_id', '')}\n- job_id: {msg_or_job.get('job_id', '')}\n- thread_id: {msg_or_job.get('thread_id', '')}\n"
    body = f"{header}{meta}\n{content.strip()}\n"
    if attachments:
        body += "\nattachments:\n" + "\n".join(f"- {a}" for a in attachments) + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(body)
    return path


def enqueue(config, job):
    path = Path(config["queue_path"])
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(job, ensure_ascii=False) + "\n")


def requested_full_access(config, text, attachments=None):
    haystack = text.lower()
    if attachments:
        haystack += " " + " ".join(str(path).lower() for path in attachments)
    return any(keyword.lower() in haystack for keyword in config.get("full_access_keywords", []))


def approval_code(job):
    return job["job_id"].split("-", 1)[-1]


def codex_permission_args(permission, resume=False):
    if resume and permission != "full":
        return []
    if permission == "read":
        return ["-s", "read-only", "-a", "never"]
    if permission == "full":
        return ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"]
    return ["-s", "danger-full-access", "-a", "never"]


def is_image_path(path):
    return Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def parse_codex_json_output(output):
    thread_id = None
    messages = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id") or thread_id
        if event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                messages.append(item["text"])
    return thread_id, "\n\n".join(messages).strip()


def chat_session(state, chat_id_value):
    return (state.get("sessions") or {}).get(str(chat_id_value), {})


def remember_chat_session(state, chat_id_value, thread_id):
    if not thread_id:
        return
    sessions = state.setdefault("sessions", {})
    sessions[str(chat_id_value)] = {"thread_id": thread_id, "updated_at": now_iso()}


def forget_chat_session(state, chat_id_value):
    state.setdefault("sessions", {}).pop(str(chat_id_value), None)


def run_codex(config, state, job, prompt):
    mode = config["codex_mode"]
    if mode == "queue":
        return None, "큐에 저장 완료. Codex 실행은 queue 모드라 자동 주입하지 않음."
    codex = os.path.expanduser(config["codex_path"])
    cwd = config["codex_cwd"]
    attachments = [p for p in job.get("attachments", []) if is_image_path(p) and Path(p).exists()]
    image_args = []
    for path in attachments:
        image_args.extend(["-i", path])
    permission = job.get("permission", config.get("default_permission", "write"))

    existing_thread = chat_session(state, job["chat_id"]).get("thread_id")
    if mode == "chat" and existing_thread:
        permission_args = codex_permission_args(permission, resume=True)
        cmd = [
            codex,
            "exec",
            "resume",
            "--json",
            "--skip-git-repo-check",
            *permission_args,
            *image_args,
            existing_thread,
            "-",
        ]
    elif mode == "resume-last":
        permission_args = codex_permission_args(permission, resume=True)
        cmd = [
            codex,
            "exec",
            "resume",
            "--json",
            "--last",
            "--skip-git-repo-check",
            *permission_args,
            *image_args,
            "-",
        ]
    else:
        permission_args = codex_permission_args(permission, resume=False)
        cmd = [
            codex,
            "exec",
            "--json",
            "--skip-git-repo-check",
            *permission_args,
            "-C",
            cwd,
            *image_args,
            "-",
        ]
    proc = subprocess.run(
        cmd,
        input=prompt,
        text=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=None,
    )
    output = proc.stdout.strip()
    if proc.returncode != 0:
        raise RuntimeError(f"Codex exited {proc.returncode}\n{output}")
    thread_id, final_text = parse_codex_json_output(output)
    if mode == "chat" and thread_id:
        remember_chat_session(state, job["chat_id"], thread_id)
    if not final_text:
        final_text = output or "Codex 실행 완료. 출력 없음."
    return thread_id, final_text


def publish_redis_result(config, job, status, result):
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        return
    payload = {
        "status": status,
        "job_id": job["job_id"],
        "task": job["text"][:120],
        "result": result,
        "source": "telegram-codex-bridge",
    }
    cmd = ["redis-cli", "-u", redis_url, "LPUSH", config["redis_result_queue"], json.dumps(payload, ensure_ascii=False)]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


def build_prompt(job):
    context_files = job.get("system_context_files", [])
    if context_files:
        context_note = "\n".join(f"- {path}" for path in context_files)
    else:
        context_note = "- No extra local context files configured."
    return f"""Telegram 대화 입력입니다.

운영규칙:
- 설정된 로컬 컨텍스트 파일과 프로젝트 규칙을 따른다.
- 필요한 경우 raw log와 handoff 문서를 갱신한다.
- Telegram으로 돌아갈 최종 답변은 간결하게 정리한다.
- 사용자가 질문에 답한 경우 이전 Telegram 대화 흐름을 이어서 이해한다.
- job_id, thread_id, queue 같은 내부 구현 용어를 사용자에게 노출하지 않는다.
- "접수됨", "처리 완료" 같은 티켓/큐 표현을 쓰지 말고 자연스럽게 대화한다.
- 현재 실행 권한은 `{job.get("permission", "write")}` 이다. Telegram 사용자가 승인한 범위 안에서 작업한다.
- job_id는 {job["job_id"]} 이다.

로컬 컨텍스트 파일:
{context_note}

보낸 사람: {job["sender_name"]} ({job["from_user_id"]})
chat_id: {job["chat_id"]}
첨부: {", ".join(job.get("attachments", [])) or "없음"}

요청:
{job["text"]}
"""


class Bridge:
    def __init__(self, config, api):
        self.config = config
        self.api = api
        self.state = load_state(config)
        self.config["codex_mode"] = self.state.get("mode", self.config["codex_mode"])
        self.jobs = queue.Queue()
        self.stop = threading.Event()

    def start(self):
        worker = threading.Thread(target=self.worker_loop, daemon=True)
        worker.start()
        self.poll_loop()

    def poll_loop(self):
        while not self.stop.is_set():
            try:
                updates = self.api.get_updates(self.state.get("offset"), self.config["poll_timeout_seconds"])
                for update in updates:
                    self.state["offset"] = update["update_id"] + 1
                    save_state(self.config, self.state)
                    msg = update.get("message")
                    if msg:
                        self.handle_message(msg)
                    callback = update.get("callback_query")
                    if callback:
                        self.handle_callback(callback)
            except KeyboardInterrupt:
                self.stop.set()
            except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
                print(f"[bridge] poll error: {exc}", file=sys.stderr)
                time.sleep(5)

    def handle_message(self, msg):
        if not is_allowed(self.config, msg):
            return
        cid = chat_id(msg)
        sid = sender_id(msg)
        text = message_text(msg).strip()
        attachments = self.download_attachments(msg)
        if not text and attachments:
            text = "첨부파일 확인하고 필요한 작업을 진행해줘."
        if not text:
            return
        if text.startswith("/"):
            self.handle_command(msg, text)
            return
        self.submit_job(msg, text, attachments)

    def submit_job(self, msg, text, attachments=None, permission=None, force=False):
        attachments = attachments or []
        cid = chat_id(msg)
        sid = sender_id(msg)
        permission = permission or self.config.get("default_permission", "write")
        job = {
            "type": "execute",
            "job_id": f"tg-{int(time.time())}-{msg['message_id']}",
            "source": "telegram",
            "chat_id": cid,
            "from_user_id": sid,
            "sender_name": sender_name(msg),
            "text": text,
            "attachments": attachments,
            "created_at": now_iso(),
            "system_context_files": self.config.get("system_context_files", []),
            "permission": permission,
        }
        channel = channel_for_user(self.config, sid)
        log_markdown(self.config, channel, "IN", job, text, attachments)
        if (
            self.config.get("require_approval_for_full", True)
            and permission != "full"
            and requested_full_access(self.config, text, attachments)
            and not force
        ):
            job["permission"] = "full"
            code = approval_code(job)
            self.state.setdefault("pending_approvals", {})[code] = job
            save_state(self.config, self.state)
            buttons = [[
                {"text": "승인", "callback_data": f"approve:{code}"},
                {"text": "취소", "callback_data": f"deny:{code}"},
            ]]
            self.api.send_message_with_buttons(
                cid,
                "이건 파일/앱/전체권한이 필요한 작업으로 보여요.\n진행할까요?",
                buttons,
                msg.get("message_id"),
                self.config["max_message_chars"],
            )
            return
        enqueue(self.config, job)
        self.jobs.put(job)
        self.state["last_chat_id"] = cid
        save_state(self.config, self.state)
        if self.config.get("send_ack"):
            self.api.send_message(cid, "보고 있어요.", msg.get("message_id"), self.config["max_message_chars"])

    def handle_command(self, msg, text):
        cid = chat_id(msg)
        parts = shlex.split(text)
        cmd = parts[0].lower()
        if cmd == "/help":
            self.api.send_message(
                cid,
                "/status\n/new [첫 메시지]\n/forget\n/mode chat|queue|resume-last|new\n/full 작업내용\n/reply 작업내용\n\n권한 승인은 버튼으로 처리돼요.",
                msg.get("message_id"),
                self.config["max_message_chars"],
            )
        elif cmd == "/status":
            thread = chat_session(self.state, cid).get("thread_id")
            pending = len(self.state.get("pending_approvals", {}))
            self.api.send_message(
                cid,
                f"mode={self.config['codex_mode']}\npermission={self.config.get('default_permission', 'write')}\nthread={thread or '없음'}\npending={pending}\nqueue={self.config['queue_path']}",
                msg.get("message_id"),
                self.config["max_message_chars"],
            )
        elif cmd == "/mode" and len(parts) >= 2:
            mode = parts[1]
            if mode not in {"chat", "queue", "resume-last", "new"}:
                self.api.send_message(cid, "지원 모드: chat, queue, resume-last, new", msg.get("message_id"), self.config["max_message_chars"])
                return
            self.config["codex_mode"] = mode
            self.state["mode"] = mode
            save_state(self.config, self.state)
            self.api.send_message(cid, f"mode={mode}", msg.get("message_id"), self.config["max_message_chars"])
        elif cmd == "/new":
            forget_chat_session(self.state, cid)
            save_state(self.config, self.state)
            rest = text.partition(" ")[2].strip()
            if rest:
                fake = dict(msg)
                fake["text"] = rest
                self.handle_message(fake)
            else:
                self.api.send_message(cid, "새 Codex 대화로 전환됨. 다음 메시지가 새 thread를 시작함.", msg.get("message_id"), self.config["max_message_chars"])
        elif cmd == "/forget":
            forget_chat_session(self.state, cid)
            save_state(self.config, self.state)
            self.api.send_message(cid, "현재 Telegram chat의 Codex thread 연결을 삭제함.", msg.get("message_id"), self.config["max_message_chars"])
        elif cmd == "/reply" and len(parts) >= 2:
            fake = dict(msg)
            fake["text"] = text.partition(" ")[2]
            self.handle_message(fake)
        elif cmd == "/full" and len(parts) >= 2:
            rest = text.partition(" ")[2].strip()
            self.submit_job(msg, rest, self.download_attachments(msg), permission="full", force=True)
        elif cmd == "/approve" and len(parts) >= 2:
            code = parts[1]
            pending = self.state.setdefault("pending_approvals", {})
            job = pending.pop(code, None)
            if not job:
                self.api.send_message(cid, "승인 대기 중인 작업을 찾지 못했어요.", msg.get("message_id"), self.config["max_message_chars"])
                save_state(self.config, self.state)
                return
            job["approved_at"] = now_iso()
            job["approved_by"] = sender_id(msg)
            job["permission"] = "full"
            enqueue(self.config, job)
            self.jobs.put(job)
            save_state(self.config, self.state)
            if self.config.get("send_ack"):
                self.api.send_message(cid, "승인 확인. 바로 진행할게요.", msg.get("message_id"), self.config["max_message_chars"])
        elif cmd == "/deny" and len(parts) >= 2:
            code = parts[1]
            pending = self.state.setdefault("pending_approvals", {})
            if pending.pop(code, None):
                save_state(self.config, self.state)
                self.api.send_message(cid, "취소했어요.", msg.get("message_id"), self.config["max_message_chars"])
            else:
                self.api.send_message(cid, "취소할 대기 작업을 찾지 못했어요.", msg.get("message_id"), self.config["max_message_chars"])
        else:
            self.api.send_message(cid, "알 수 없는 명령. /help", msg.get("message_id"), self.config["max_message_chars"])

    def handle_callback(self, callback):
        user = callback.get("from") or {}
        sid = str(user.get("id", ""))
        if sid not in set(map(str, self.config["allowed_user_ids"])):
            self.api.answer_callback_query(callback["id"], "권한 없음")
            return
        data = callback.get("data") or ""
        message = callback.get("message") or {}
        cid = str((message.get("chat") or {}).get("id", ""))
        message_id = message.get("message_id")
        action, _, code = data.partition(":")
        if action not in {"approve", "deny"} or not code:
            self.api.answer_callback_query(callback["id"], "알 수 없는 동작")
            return

        pending = self.state.setdefault("pending_approvals", {})
        job = pending.pop(code, None)
        if not job:
            self.api.answer_callback_query(callback["id"], "이미 처리됐거나 만료됨")
            if cid and message_id:
                try:
                    self.api.edit_message_text(cid, message_id, "이미 처리됐거나 만료된 요청이에요.", self.config["max_message_chars"])
                except Exception:
                    pass
            save_state(self.config, self.state)
            return

        if action == "deny":
            save_state(self.config, self.state)
            self.api.answer_callback_query(callback["id"], "취소됨")
            if cid and message_id:
                try:
                    self.api.edit_message_text(cid, message_id, "취소했어요.", self.config["max_message_chars"])
                except Exception:
                    pass
            return

        job["approved_at"] = now_iso()
        job["approved_by"] = sid
        job["permission"] = "full"
        enqueue(self.config, job)
        self.jobs.put(job)
        save_state(self.config, self.state)
        self.api.answer_callback_query(callback["id"], "승인됨")
        if cid and message_id:
            try:
                self.api.edit_message_text(cid, message_id, "승인 확인. 바로 진행할게요.", self.config["max_message_chars"])
            except Exception:
                pass

    def download_attachments(self, msg):
        paths = []
        for kind, file_id, name in attachment_file_ids(msg):
            try:
                file_path = self.api.get_file_path(file_id)
                dest = Path(self.config["attachment_dir"]) / today() / f"{msg['message_id']}_{kind}_{safe_name(name)}"
                self.api.download_file(file_path, dest)
                paths.append(str(dest))
            except Exception as exc:
                paths.append(f"download_failed:{kind}:{exc}")
        return paths

    def worker_loop(self):
        while not self.stop.is_set():
            try:
                job = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.process_job(job)
            except Exception as exc:
                text = f"지금 이쪽에서 실행이 막혔어요.\n\n{exc}"
                self.api.send_message(job["chat_id"], text, None, self.config["max_message_chars"])
                publish_redis_result(self.config, job, "cannot", str(exc))
            finally:
                self.jobs.task_done()

    def process_job(self, job):
        stop_typing = threading.Event()
        typing = threading.Thread(target=self.typing_loop, args=(job["chat_id"], stop_typing), daemon=True)
        typing.start()
        prompt = build_prompt(job)
        try:
            thread_id, result = run_codex(self.config, self.state, job, prompt)
            save_state(self.config, self.state)
            channel = channel_for_user(self.config, job["from_user_id"])
            if thread_id:
                job["thread_id"] = thread_id
            log_markdown(self.config, channel, "OUT", job, result)
            publish_redis_result(self.config, job, "done", result)
            self.api.send_message(job["chat_id"], result, None, self.config["max_message_chars"])
        finally:
            stop_typing.set()

    def typing_loop(self, chat_id_value, stop_event):
        interval = max(1, int(self.config.get("typing_interval_seconds", 5)))
        while not stop_event.is_set():
            try:
                self.api.send_chat_action(chat_id_value)
            except Exception:
                pass
            stop_event.wait(interval)


def enqueue_cli(config, text):
    job = {
        "type": "execute",
        "job_id": f"manual-{int(time.time())}",
        "source": "manual",
        "chat_id": "",
        "from_user_id": "manual",
        "sender_name": "manual",
        "text": text,
        "attachments": [],
        "created_at": now_iso(),
        "system_context_files": config.get("system_context_files", []),
    }
    enqueue(config, job)
    print(job["job_id"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "enqueue", "test-token"])
    parser.add_argument("text", nargs="*")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    fallback_env = os.environ.get("TELEGRAM_CODEX_BRIDGE_ENV")
    if fallback_env:
        load_dotenv(Path(fallback_env).expanduser())
    config = load_config()

    if args.command == "enqueue":
        enqueue_cli(config, " ".join(args.text))
        return

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN 없음. .env 또는 ~/.claude/channels/telegram/.env 확인 필요.")
    api = TelegramAPI(token)

    if args.command == "test-token":
        try:
            me = api.call("getMe")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise SystemExit("Telegram token rejected: 401 Unauthorized. BotFather에서 새 토큰을 발급해 .env에 넣어야 함.")
            if exc.code == 404:
                raise SystemExit("Telegram token rejected: 404 Not Found. BotFather 토큰 전체 형식은 보통 '숫자:AA...' 이며, 콜론 앞 bot id가 빠졌는지 확인 필요.")
            raise
        print(json.dumps({"ok": True, "username": me.get("username"), "id": me.get("id")}, ensure_ascii=False))
        return

    Bridge(config, api).start()


if __name__ == "__main__":
    main()
