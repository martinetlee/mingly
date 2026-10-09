"""Signal connector against a fake ``signal-cli``: detection, device linking,
the supervised jsonRpc runtime (ingest of direct / group / sync-sent
messages, edits, remote deletes, reactions, receipts), send with quote,
reactions, attachment fetch and restart after a crash.

The fake is a small Python program speaking signal-cli's CLI and its
newline-delimited JSON-RPC 2.0 protocol. Each test writes a scenario
(``fake.json`` in the signal config dir) that the fake reads; the fake logs
its argv and every JSON-RPC request so tests can assert what was sent.
"""

import asyncio
import json
import os
import stat
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from backend import config
from backend.connectors.base import OutgoingFile, OutgoingMessage
from backend.connectors.manager import ConnectorManager
from backend.connectors.signal import cli, register
from backend.connectors.signal.connector import SignalConnector
from backend.connectors.signal.envelopes import Address, MessageEvent, parse_envelope
from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Account, Conversation, Identity, Message, MessageAttachment
from backend.ingest import fanout

SELF_NUMBER = "+15550001111"
SELF_ACI = "aaaaaaaa-0000-4000-8000-000000000001"
ALICE = "bbbbbbbb-0000-4000-8000-000000000002"
ALICE_NUMBER = "+15550002222"
BOB = "cccccccc-0000-4000-8000-000000000003"
GROUP = "Z3JvdXAtaWQtYmFzZTY0PQ=="
SEND_TS = 1700000009000

FAKE_SIGNAL_CLI = r'''
import json, os, sys, time

args = sys.argv[1:]
if args == ["--version"]:
    if os.environ.get("FAKE_SIGNAL_BROKEN"):
        print("Error: could not find java", file=sys.stderr)
        sys.exit(1)
    print("signal-cli 0.13.9")
    sys.exit(0)

cfg = account = None
rest = []
i = 0
while i < len(args):
    if args[i] in ("--config", "-a", "-o"):
        if args[i] == "--config":
            cfg = args[i + 1]
        elif args[i] == "-a":
            account = args[i + 1]
        i += 2
        continue
    rest.append(args[i])
    i += 1
cmd = rest[0]

def path(name):
    return os.path.join(cfg, name)

scenario = json.load(open(path("fake.json"))) if os.path.exists(path("fake.json")) else {}
with open(path("fake_argv.jsonl"), "a") as fh:
    fh.write(json.dumps(args) + "\n")

if cmd == "link":
    print("Some JVM warning line", flush=True)
    print("sgnl://linkdevice?uuid=abc123&pub_key=xyz", flush=True)
    time.sleep(scenario.get("link_delay", 0.3))
    if scenario.get("link_fail"):
        print("Link request error: Connection closed!", file=sys.stderr, flush=True)
        sys.exit(3)
    os.makedirs(path("data"), exist_ok=True)
    with open(path("data/accounts.json"), "w") as fh:
        json.dump({"accounts": [{"path": "1", "number": scenario["number"], "uuid": scenario["aci"]}],
                   "version": 2}, fh)
    print("Associated with: " + scenario["number"], flush=True)
    sys.exit(0)

if cmd == "listAccounts":
    try:
        data = json.load(open(path("data/accounts.json")))
    except OSError:
        data = {"accounts": []}
    print(json.dumps([{"number": a["number"]} for a in data["accounts"]]))
    sys.exit(0)

if cmd in ("unregister", "deleteLocalAccountData"):
    sys.exit(0)

if cmd == "jsonRpc":
    runs = int(open(path("fake_runs")).read()) + 1 if os.path.exists(path("fake_runs")) else 1
    open(path("fake_runs"), "w").write(str(runs))
    if runs <= scenario.get("crash_runs", 0):
        print("ERROR App - boom (run %d)" % runs, file=sys.stderr, flush=True)
        sys.exit(1)

    def out(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    group_lists = 0
    while True:
        line = sys.stdin.readline()
        if not line:
            sys.exit(0)
        req = json.loads(line)
        with open(path("fake_rpc.jsonl"), "a") as fh:
            fh.write(json.dumps(req) + "\n")
        method = req["method"]
        if method == "listContacts":
            result = scenario.get("contacts", [])
        elif method == "listGroups":
            group_lists += 1
            result = scenario.get("groups" if group_lists == 1 else "groups_later", scenario.get("groups", []))
        elif method in ("send", "sendReaction", "sendReceipt"):
            result = {"timestamp": scenario.get("send_ts", 1), "results": [{"type": "SUCCESS"}]}
        else:
            out({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": "no " + method}})
            continue
        out({"jsonrpc": "2.0", "id": req["id"], "result": result})
        if method == "listGroups" and group_lists == 1:
            for env in scenario.get("envelopes", []):
                out({"jsonrpc": "2.0", "method": "receive", "params": {"envelope": env, "account": account}})
    sys.exit(0)

print("unknown command " + cmd, file=sys.stderr)
sys.exit(2)
'''


# ── fixtures ─────────────────────────────────────────────────────────────────

class _Capture:
    def __init__(self):
        self.events = []

    async def __call__(self, data):
        self.events.append(data)


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    """Install the fake as the signal-cli executable."""
    exe = tmp_path / "bin" / "signal-cli"
    exe.parent.mkdir()
    exe.write_text(f"#!{sys.executable}\n{FAKE_SIGNAL_CLI}")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(cli, "find_signal_cli", lambda: str(exe))
    return exe


@pytest.fixture
def events():
    """Capture WS broadcasts and stub fan-out side effects (scam, notifications)."""
    capture = _Capture()
    with ExitStack() as stack:
        stack.enter_context(patch("backend.ws.manager.ws_manager.broadcast", capture))
        stack.enter_context(patch.object(fanout, "enqueue_scam", AsyncMock()))
        stack.enter_context(patch.object(fanout, "send_native_notification", MagicMock()))
        stack.enter_context(patch.object(fanout, "stamp_triage", AsyncMock()))
        stack.enter_context(patch("backend.prefs.get_notif_pref", return_value="all"))
        yield capture


def _scenario(**kw) -> None:
    data = {"number": SELF_NUMBER, "aci": SELF_ACI, "send_ts": SEND_TS}
    data.update(kw)
    (cli.signal_dir() / "fake.json").write_text(json.dumps(data))


def _linked() -> None:
    """signal-cli state as left by a successful link."""
    data = cli.signal_dir() / "data"
    data.mkdir(exist_ok=True)
    (data / "accounts.json").write_text(json.dumps(
        {"accounts": [{"path": "1", "number": SELF_NUMBER, "uuid": SELF_ACI}], "version": 2}
    ))


def _rpc_log() -> list:
    p = cli.signal_dir() / "fake_rpc.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


async def _until(cond, timeout=15.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        res = await cond()
        if res:
            return res
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.05)


async def _account(db) -> Account:
    acc = Account(channel="signal", external_id=SELF_NUMBER, label=SELF_NUMBER)
    db.add(acc)
    await db.commit()
    return acc


async def _messages(conv_external_id=None) -> list:
    async with AsyncSessionLocal() as s:
        q = select(Message).join(Conversation, Conversation.id == Message.conversation_id).order_by(Message.seq)
        if conv_external_id is not None:
            q = q.where(Conversation.external_id == conv_external_id)
        return list((await s.execute(q)).scalars().all())


async def _running(db, **scenario) -> SignalConnector:
    _linked()
    _scenario(**scenario)
    acc = await _account(db)
    conn = SignalConnector(acc.id)
    conn.backoff_initial = 0.05
    assert conn.is_configured()
    assert await conn.start() is True
    await _until(lambda: _async(conn.status().state == "ready"))
    return conn


async def _async(value):
    return value


def _env(ts, **kw) -> dict:
    env = {"source": ALICE, "sourceUuid": ALICE, "sourceNumber": ALICE_NUMBER, "sourceName": "Alice A",
           "sourceDevice": 1, "timestamp": ts}
    env.update(kw)
    return env


CONTACTS = [
    {"number": ALICE_NUMBER, "uuid": ALICE, "name": "Alice Address-Book", "profile": {"givenName": "Alice"}},
    {"number": None, "uuid": BOB, "name": "", "profile": {"givenName": "Bob", "familyName": "B"}},
]
GROUPS = [{"id": GROUP, "name": "Climbing crew", "members": [{"uuid": ALICE}, {"uuid": BOB}, {"uuid": SELF_ACI}]}]


# ── detection ────────────────────────────────────────────────────────────────

@pytest.fixture
def no_install(monkeypatch):
    """No signal-cli anywhere and a fresh setup state; returns a switch that
    makes the fake signal-cli appear (what a finished setup does)."""
    from backend.connectors.signal import install

    monkeypatch.setattr(install, "_state", install.InstallState())
    monkeypatch.setattr(install, "_task", None)
    found = {"exe": None}
    monkeypatch.setattr(cli, "find_signal_cli", lambda: found["exe"])

    def appear(exe):
        found["exe"] = str(exe)

    return appear


def _fake_exe(tmp_path) -> Path:
    exe = tmp_path / "bin" / "signal-cli"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text(f"#!{sys.executable}\n{FAKE_SIGNAL_CLI}")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


@pytest.mark.asyncio
async def test_missing_signal_cli_offers_setup_then_links(no_install, tmp_path, monkeypatch):
    from backend.connectors.signal import install

    started = []

    def fake_start():
        started.append(1)
        install._state = install.InstallState(status="downloading", downloaded=50_000_000, total=180_000_000)
        return install._state

    monkeypatch.setattr(install, "start", fake_start)
    manager = ConnectorManager()
    register(manager)

    step = await manager.begin_auth("signal", {})
    assert step.kind == "fields" and step.fields == [] and step.poll is False
    assert step.title == "Set up Signal support" and step.submit_label == "Set up"
    assert "brew" not in step.message and "MB" in step.message

    step = await manager.continue_auth(step.flow_id, {})
    assert step.kind == "progress" and step.poll is True
    assert "50 of 180 MB" in step.message and 0 < step.progress < 0.9
    assert started == [1]

    # Polling while it downloads doesn't start another setup
    step = await manager.continue_auth(step.flow_id, {})
    assert step.kind == "progress" and started == [1]

    # Setup done: the same flow moves on to the QR code
    _scenario()
    no_install(_fake_exe(tmp_path))
    install._state.status = "done"
    flow_id = step.flow_id
    step = await manager.continue_auth(flow_id, {})
    assert step.kind == "qr" and step.flow_id == flow_id
    assert step.qr_payload == "sgnl://linkdevice?uuid=abc123&pub_key=xyz"


@pytest.mark.asyncio
async def test_failed_setup_is_an_error_step(no_install, monkeypatch):
    from backend.connectors.signal import install

    def fake_start():
        install._state = install.InstallState(status="error", error="Download failed (ConnectError).")
        return install._state

    monkeypatch.setattr(install, "start", fake_start)
    connector = SignalConnector()
    step = await connector.begin_auth({})
    step = await connector.continue_auth(step.flow_id, {})
    assert step.kind == "error" and "Download failed" in step.message


@pytest.mark.asyncio
async def test_opening_signal_while_setup_runs_shows_progress(no_install):
    from backend.connectors.signal import install

    install._state = install.InstallState(status="unpacking", total=1)
    step = await SignalConnector().begin_auth({})
    assert step.kind == "progress" and step.poll is True and "Unpacking" in step.message


@pytest.mark.asyncio
async def test_signal_cli_that_cannot_run_offers_repair(fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_SIGNAL_BROKEN", "1")
    monkeypatch.setattr(cli, "java_available", lambda: False)
    step = await SignalConnector().begin_auth({})
    assert step.kind == "fields" and step.title == "Repair Signal support" and step.submit_label == "Repair"
    assert "no Java runtime" in step.message


def test_homebrew_dirs_are_searched_and_put_on_path(tmp_path, monkeypatch):
    brew = tmp_path / "brew"
    brew.mkdir()
    exe = brew / "signal-cli"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(cli.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(cli, "HOMEBREW_BIN_DIRS", (str(brew),))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert cli.find_signal_cli() == str(exe)
    assert cli.child_env()["PATH"].split(os.pathsep)[0] == str(brew)


# ── linking ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_link_flow_creates_account_and_self_identity(db, fake_cli, events):
    _scenario()
    manager = ConnectorManager()
    register(manager)
    assert {"channel": "signal", "display_name": "Signal", "multi_account": True} in manager.channels()

    step = await manager.begin_auth("signal", {})
    assert step.kind == "qr" and step.poll is True
    assert step.qr_payload == "sgnl://linkdevice?uuid=abc123&pub_key=xyz"
    argv = json.loads((cli.signal_dir() / "fake_argv.jsonl").read_text().splitlines()[0])
    assert argv == ["--config", str(cli.signal_dir()), "link", "-n", "Mingly"]
    assert stat.S_IMODE(os.stat(cli.signal_dir()).st_mode) == 0o700

    async def poll():
        s = await manager.continue_auth(step.flow_id, {})
        return s if s.kind != "qr" else None
    done = await _until(poll)
    assert done.kind == "done", done.message
    assert SELF_NUMBER in done.message and "from the moment it is linked" in done.message

    async with AsyncSessionLocal() as s:
        acc = (await s.execute(select(Account).where(Account.channel == "signal"))).scalar_one()
        me = await crud.get_identity(s, acc.self_identity_id)
        person = await crud.get_person(s, me.person_id)
    assert (acc.external_id, acc.label) == (SELF_NUMBER, SELF_NUMBER)
    assert (me.channel, me.external_id, me.phone, me.is_self) == ("signal", SELF_ACI, SELF_NUMBER, True)
    assert person.is_self

    connector = manager.get(f"signal:{acc.id}")
    assert connector is not None and connector.account_id == acc.id
    await _until(lambda: _async(connector.status().state == "ready"))  # the manager started it
    await connector.stop()
    assert connector.status().state == "stopped"


@pytest.mark.asyncio
async def test_link_failure_is_an_error_step(db, fake_cli):
    _scenario(link_fail=True)
    conn = SignalConnector()
    step = await conn.begin_auth({})
    assert step.kind == "qr"

    async def poll():
        s = await conn.continue_auth(step.flow_id, {})
        return s if s.kind != "qr" else None
    err = await _until(poll)
    assert err.kind == "error" and "Connection closed" in err.message
    assert conn.account_id is None


# ── runtime ingest ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_runtime_ingests_direct_group_and_sync_sent(db, fake_cli, events):
    envelopes = [
        _env(1000, dataMessage={
            "timestamp": 1000, "message": "hi from alice",
            "attachments": [{"contentType": "image/jpeg", "filename": "cat.jpg", "id": "att1.jpg", "size": 12}],
        }),
        _env(2000, source=BOB, sourceUuid=BOB, sourceNumber=None, sourceName="Bob B", dataMessage={
            "timestamp": 2000, "message": "hey ￼ look",
            "mentions": [{"name": "Alice", "uuid": ALICE, "start": 4, "length": 1}],
            "groupInfo": {"groupId": GROUP, "type": "DELIVER"},
            "quote": {"id": 1000, "author": ALICE, "authorUuid": ALICE, "text": "hi from alice"},
        }),
        {"source": SELF_ACI, "sourceUuid": SELF_ACI, "sourceNumber": SELF_NUMBER, "timestamp": 3000,
         "syncMessage": {"sentMessage": {"destination": ALICE_NUMBER, "destinationNumber": ALICE_NUMBER,
                                         "destinationUuid": ALICE, "timestamp": 3000, "message": "sent from phone"}}},
    ]
    conn = await _running(db, contacts=CONTACTS, groups=GROUPS, envelopes=envelopes)
    try:
        msgs = await _until(lambda: _msgs_when(3))
    finally:
        await conn.stop()

    direct, group, sent = msgs
    async with AsyncSessionLocal() as s:
        convs = {c.external_id: c for c in (await s.execute(select(Conversation))).scalars().all()}
        alice = (await s.execute(select(Identity).where(Identity.external_id == ALICE))).scalar_one()
        me = (await s.execute(select(Identity).where(Identity.external_id == SELF_ACI))).scalar_one()
        att = (await s.execute(select(MessageAttachment))).scalar_one()

    dconv, gconv = convs[ALICE], convs[GROUP]
    assert (dconv.kind, dconv.title, dconv.peer_identity_id) == ("direct", "Alice Address-Book", alice.id)
    assert (gconv.kind, gconv.title, gconv.members_count) == ("group", "Climbing crew", 3)
    assert alice.phone == ALICE_NUMBER and alice.is_contact

    assert (direct.conversation_id, direct.external_id, direct.seq) == (dconv.id, f"{ALICE}:1000", 1000)
    assert direct.text == "hi from alice" and not direct.is_outgoing
    assert direct.sender_identity_id == alice.id
    assert (att.kind, att.filename, att.external_ref, att.message_id) == ("photo", "cat.jpg", "att1.jpg", direct.id)

    assert group.conversation_id == gconv.id and group.external_id == f"{BOB}:2000"
    assert group.text == "hey @Alice look" and group.sender_name == "Bob B"
    assert group.reply_to_external_id == f"{ALICE}:1000"

    assert sent.conversation_id == dconv.id and sent.is_outgoing
    assert sent.external_id == f"{SELF_ACI}:3000" and sent.sender_identity_id == me.id

    new = [e for e in events.events if e["type"] == "new_message"]
    assert sorted(e["message_id"] for e in new) == sorted(m.id for m in msgs)
    status = [e["state"] for e in events.events if e["type"] == "connector_status"]
    assert status[:3] == ["starting", "syncing", "ready"] and status[-1] == "stopped"


async def _msgs_when(n, conv_external_id=None):
    msgs = await _messages(conv_external_id)
    return msgs if len(msgs) >= n else None


@pytest.mark.asyncio
async def test_runtime_edits_reactions_receipts_and_remote_delete(db, fake_cli, events):
    sync = {"source": SELF_ACI, "sourceUuid": SELF_ACI, "sourceNumber": SELF_NUMBER}
    envelopes = [
        _env(1000, dataMessage={"timestamp": 1000, "message": "original"}),
        _env(1100, dataMessage={"timestamp": 1100, "message": "to be deleted"}),
        dict(sync, timestamp=1200, syncMessage={"sentMessage": {
            "destinationUuid": ALICE, "destinationNumber": ALICE_NUMBER, "timestamp": 1200, "message": "mine"}}),
        # Alice edits her first message, then reacts to mine and deletes her second.
        _env(1300, editMessage={"targetSentTimestamp": 1000,
                                "dataMessage": {"timestamp": 1300, "message": "edited text"}}),
        _env(1400, dataMessage={"timestamp": 1400, "reaction": {
            "emoji": "❤️", "targetAuthor": SELF_ACI, "targetAuthorUuid": SELF_ACI,
            "targetSentTimestamp": 1200, "isRemove": False}}),
        dict(sync, timestamp=1450, syncMessage={"sentMessage": {
            "destinationUuid": ALICE, "timestamp": 1450, "reaction": {
                "emoji": "👍", "targetAuthorUuid": ALICE, "targetSentTimestamp": 1000, "isRemove": False}}}),
        _env(1500, dataMessage={"timestamp": 1500, "remoteDelete": {"timestamp": 1100}}),
        _env(1600, receiptMessage={"when": 1600, "isDelivery": False, "isRead": True, "timestamps": [1200]}),
    ]
    conn = await _running(db, contacts=CONTACTS, groups=[], envelopes=envelopes)
    try:
        async def settled():
            msgs = await _messages()
            mine = [m for m in msgs if m.external_id == f"{SELF_ACI}:1200"]
            return msgs if mine and mine[0].delivery_state == "read" else None
        msgs = await _until(settled)
    finally:
        await conn.stop()

    by_ext = {m.external_id: m for m in msgs}
    assert f"{ALICE}:1100" not in by_ext  # remote delete
    edited = by_ext[f"{ALICE}:1000"]
    assert edited.text == "edited text" and edited.is_edited
    assert json.loads(edited.reactions_json) == [{"emoji": "👍", "count": 1}] and edited.my_reaction == "👍"
    mine = by_ext[f"{SELF_ACI}:1200"]
    assert json.loads(mine.reactions_json) == [{"emoji": "❤️", "count": 1}] and mine.my_reaction is None
    assert mine.delivery_state == "read"

    types = [e["type"] for e in events.events]
    assert "messages_deleted" in types and types.count("message_edited") >= 4


@pytest.mark.asyncio
async def test_group_joined_after_start_is_listed_again_for_its_title(db, fake_cli, events):
    conn = await _running(db, contacts=[], groups=[], groups_later=GROUPS, envelopes=[
        _env(1000, dataMessage={"timestamp": 1000, "message": "welcome",
                                "groupInfo": {"groupId": GROUP, "type": "DELIVER"}}),
        _env(1001, dataMessage={"timestamp": 1001, "message": "again",
                                "groupInfo": {"groupId": GROUP, "type": "DELIVER"}}),
    ])
    try:
        await _until(lambda: _msgs_when(2))
    finally:
        await conn.stop()
    async with AsyncSessionLocal() as s:
        conv = (await s.execute(select(Conversation))).scalar_one()
    assert (conv.kind, conv.title, conv.members_count) == ("group", "Climbing crew", 3)
    assert [r["method"] for r in _rpc_log()].count("listGroups") == 2


@pytest.mark.asyncio
async def test_reaction_removal_and_redelivery_keep_counts(db, fake_cli, events):
    conn = await _running(db, contacts=[], groups=[], envelopes=[
        _env(1000, dataMessage={"timestamp": 1000, "message": "x"}),
        _env(1001, dataMessage={"timestamp": 1001, "reaction": {
            "emoji": "😂", "targetAuthorUuid": ALICE, "targetSentTimestamp": 1000}}),
        _env(1000, dataMessage={"timestamp": 1000, "message": "x"}),  # re-delivered
    ])
    try:
        await _until(lambda: _reacted(f"{ALICE}:1000"))
        await asyncio.sleep(0.3)  # let the re-delivery land
        [msg] = await _messages()
        assert json.loads(msg.reactions_json) == [{"emoji": "😂", "count": 1}]
        await conn.handle_receive({"envelope": _env(1002, dataMessage={"timestamp": 1002, "reaction": {
            "emoji": "😂", "targetAuthorUuid": ALICE, "targetSentTimestamp": 1000, "isRemove": True}})})
        [msg] = await _messages()
        assert msg.reactions_json is None and msg.meta_json is None
    finally:
        await conn.stop()


async def _reacted(ext):
    msgs = [m for m in await _messages() if m.external_id == ext]
    return msgs and msgs[0].reactions_json


# ── outgoing ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_with_quote_and_file(db, fake_cli, events, tmp_path):
    conn = await _running(db, contacts=CONTACTS, groups=[], envelopes=[
        _env(1000, dataMessage={"timestamp": 1000, "message": "question?"}),
    ])
    try:
        [incoming] = await _until(lambda: _msgs_when(1))
        async with AsyncSessionLocal() as s:
            conv = await crud.get_conversation(s, incoming.conversation_id)
        upload = tmp_path / "upload.tmp"
        upload.write_bytes(b"PDFDATA")
        ids = await conn.send(conv, OutgoingMessage(
            text="answer", reply_to_message_id=incoming.id,
            files=[OutgoingFile(path=str(upload), filename="doc.pdf", mime_type="application/pdf")],
        ))
    finally:
        await conn.stop()

    [req] = [r for r in _rpc_log() if r["method"] == "send"]
    assert req["params"] == {
        "recipient": [ALICE], "message": "answer", "attachments": [str(upload)],
        "quoteTimestamp": 1000, "quoteAuthor": ALICE, "quoteMessage": "question?",
    }
    async with AsyncSessionLocal() as s:
        [sent] = (await crud.get_messages_by_ids(s, ids)).values()
        att = (await s.execute(select(MessageAttachment).where(MessageAttachment.message_id == sent.id))).scalar_one()
    assert sent.external_id == f"{SELF_ACI}:{SEND_TS}" and sent.seq == SEND_TS and sent.is_outgoing
    assert sent.reply_to_external_id == f"{ALICE}:1000" and sent.text == "answer"
    assert (att.kind, att.filename, att.mime_type, att.size) == ("document", "doc.pdf", "application/pdf", 7)
    assert att.local_path == f"media/{conv.id}/{att.id}.pdf"
    assert (Path(config.DATA_DIR) / att.local_path).read_bytes() == b"PDFDATA"
    assert any(e["type"] == "media_ready" and e["attachment_id"] == att.id for e in events.events)


@pytest.mark.asyncio
async def test_group_send_and_react(db, fake_cli, events):
    conn = await _running(db, contacts=CONTACTS, groups=GROUPS, envelopes=[
        _env(1000, dataMessage={"timestamp": 1000, "message": "group hi",
                                "groupInfo": {"groupId": GROUP, "type": "DELIVER"}}),
    ])
    try:
        [msg] = await _until(lambda: _msgs_when(1))
        async with AsyncSessionLocal() as s:
            conv = await crud.get_conversation(s, msg.conversation_id)
        await conn.send(conv, OutgoingMessage(text="to the group"))
        await conn.react(conv, msg, "🔥")
        async with AsyncSessionLocal() as s:
            msg = await crud.get_message(s, msg.id)
        assert msg.my_reaction == "🔥" and json.loads(msg.reactions_json) == [{"emoji": "🔥", "count": 1}]
        await conn.react(conv, msg, None)
        async with AsyncSessionLocal() as s:
            msg = await crud.get_message(s, msg.id)
        assert msg.my_reaction is None and msg.reactions_json is None
    finally:
        await conn.stop()

    sends = [r["params"] for r in _rpc_log() if r["method"] == "send"]
    assert sends == [{"groupId": GROUP, "message": "to the group"}]
    reactions = [r["params"] for r in _rpc_log() if r["method"] == "sendReaction"]
    assert reactions == [
        {"groupId": GROUP, "emoji": "🔥", "targetAuthor": ALICE, "targetTimestamp": 1000, "remove": False},
        {"groupId": GROUP, "emoji": "🔥", "targetAuthor": ALICE, "targetTimestamp": 1000, "remove": True},
    ]


@pytest.mark.asyncio
async def test_send_requires_running_runtime(db):
    acc = await _account(db)
    conn = SignalConnector(acc.id)
    with pytest.raises(RuntimeError):
        await conn.send(MagicMock(kind="direct", external_id=ALICE), OutgoingMessage(text="x"))


# ── attachments ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_attachment_copies_from_signal_cli_storage(db):
    acc = await _account(db)
    conn = SignalConnector(acc.id)
    async with AsyncSessionLocal() as s:
        conv = await crud.upsert_conversation(s, account_id=acc.id, external_id=ALICE, title="Alice", kind="direct")
        await crud.save_messages(s, [{
            "conversation_id": conv.id, "external_id": f"{ALICE}:1", "seq": 1, "text": None,
            "attachments": [{"kind": "voice", "filename": None, "mime_type": "audio/aac", "size": 3,
                             "external_ref": "voice123"}],
        }])
        att = (await s.execute(select(MessageAttachment))).scalar_one()
        msg = await crud.get_message(s, att.message_id)
    store = cli.attachments_dir()
    store.mkdir(parents=True, exist_ok=True)
    (store / "voice123").write_bytes(b"AAC")

    rel = await conn.fetch_attachment(att, msg, conv)
    assert rel.startswith(f"media/{conv.id}/{att.id}") and rel.endswith((".aac", ".adts"))
    assert (Path(config.DATA_DIR) / rel).read_bytes() == b"AAC"
    async with AsyncSessionLocal() as s:
        stored = (await s.execute(select(MessageAttachment))).scalar_one()
    assert stored.local_path == rel

    from backend.telegram.media import MediaNotFound
    att.external_ref = "../vault.enc"
    with pytest.raises(MediaNotFound):
        await conn.fetch_attachment(att, msg, conv)
    att.external_ref = "gone"
    with pytest.raises(MediaNotFound):
        await conn.fetch_attachment(att, msg, conv)


# ── supervision ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_supervisor_restarts_after_crash(db, fake_cli, events):
    conn = await _running(db, crash_runs=2, contacts=[], groups=[], envelopes=[
        _env(1000, dataMessage={"timestamp": 1000, "message": "after restart"}),
    ])
    try:
        [msg] = await _until(lambda: _msgs_when(1))
        assert msg.text == "after restart"
        assert (cli.signal_dir() / "fake_runs").read_text() == "3"
        reconnecting = [e for e in events.events if e["type"] == "connector_status" and e["state"] == "reconnecting"]
        assert len(reconnecting) == 2 and "boom" in reconnecting[0]["error"]
        proc = conn._rpc._proc
    finally:
        await conn.stop()
    assert proc.returncode is not None  # stop() ended the child


@pytest.mark.asyncio
async def test_start_without_link_is_unauthorized(db, fake_cli, events):
    _scenario()
    (cli.signal_dir() / "data").mkdir()
    acc = await _account(db)
    conn = SignalConnector(acc.id)
    assert await conn.start() is False
    assert conn.status().state == "unauthorized"


# ── envelope parsing ─────────────────────────────────────────────────────────

def test_parse_envelope_number_only_sender_and_unknown_kinds():
    me = Address(SELF_ACI, SELF_NUMBER)
    [ev] = parse_envelope({"source": "+15559990000", "timestamp": 5, "dataMessage": {"timestamp": 5, "message": "yo"}}, me)
    assert isinstance(ev, MessageEvent)
    assert (ev.conv.kind, ev.conv.external_id, ev.external_id) == ("direct", "+15559990000", "+15559990000:5")
    assert parse_envelope({"sourceUuid": ALICE, "timestamp": 6, "typingMessage": {"action": "STARTED"}}, me) == []
    assert parse_envelope({"sourceUuid": ALICE, "timestamp": 7, "dataMessage": {
        "timestamp": 7, "groupInfo": {"groupId": GROUP, "type": "UPDATE"}}}, me) == []


@pytest.mark.asyncio
async def test_rpc_stop_terminates_a_child_that_ignores_eof():
    from backend.connectors.signal.rpc import JsonRpcProcess, ProcessGone

    # Ignores stdin EOF and SIGTERM: only SIGKILL ends it.
    rpc = JsonRpcProcess([sys.executable, "-c",
                          "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
                         AsyncMock())
    await rpc.start()
    await asyncio.sleep(0.3)
    pending = asyncio.ensure_future(rpc.call("listGroups"))
    await asyncio.sleep(0.05)
    await rpc.stop(timeout=0.3)
    assert rpc._proc.returncode == -9
    with pytest.raises(ProcessGone):
        await pending


# ── API routes ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_react_and_mark_read_through_the_api(api, fake_cli, events):
    from backend.connectors.manager import manager

    async with AsyncSessionLocal() as s:
        conn = await _running(s, contacts=CONTACTS, groups=GROUPS, envelopes=[
            _env(1000, dataMessage={"timestamp": 1000, "message": "one"}),
            _env(1001, dataMessage={"timestamp": 1001, "message": "two"}),
            _env(1002, source=BOB, sourceUuid=BOB, sourceNumber=None, dataMessage={
                "timestamp": 1002, "message": "group", "groupInfo": {"groupId": GROUP, "type": "DELIVER"}}),
            _env(1003, source=BOB, sourceUuid=BOB, sourceNumber=None, dataMessage={
                "timestamp": 1003, "message": "group 2", "groupInfo": {"groupId": GROUP, "type": "DELIVER"}}),
        ])
    manager.register(conn)
    try:
        msgs = await _until(lambda: _msgs_when(4))
        first = msgs[0]
        r = await api.post(f"/api/messages/{first.id}/react", json={"emoji": "👍"})
        assert r.status_code == 200, r.text
        assert r.json()["my_reaction"] == "👍"
        async with AsyncSessionLocal() as s:
            stored = await crud.get_message(s, first.id)
        assert stored.my_reaction == "👍" and json.loads(stored.reactions_json) == [{"emoji": "👍", "count": 1}]
        r = await api.post(f"/api/messages/{first.id}/react", json={"emoji": None})
        assert r.status_code == 200 and r.json()["my_reaction"] is None

        direct_id, group_id = first.conversation_id, msgs[2].conversation_id
        assert (await api.post(f"/api/conversations/{direct_id}/mark-read")).status_code == 200
        assert (await api.post(f"/api/conversations/{group_id}/mark-read")).status_code == 200
        # Nothing new since: no second receipt.
        assert (await api.post(f"/api/conversations/{direct_id}/mark-read")).status_code == 200
        # A newer message is receipted alone.
        await conn.handle_receive({"envelope": _env(1004, dataMessage={"timestamp": 1004, "message": "three"})})
        assert (await api.post(f"/api/conversations/{direct_id}/mark-read")).status_code == 200
    finally:
        manager._connectors.pop(conn.account_key, None)
        await conn.stop()

    reactions = [r["params"] for r in _rpc_log() if r["method"] == "sendReaction"]
    assert reactions == [
        {"recipient": [ALICE], "emoji": "👍", "targetAuthor": ALICE, "targetTimestamp": 1000, "remove": False},
        {"recipient": [ALICE], "emoji": "👍", "targetAuthor": ALICE, "targetTimestamp": 1000, "remove": True},
    ]
    receipts = [r["params"] for r in _rpc_log() if r["method"] == "sendReceipt"]
    assert receipts == [
        {"recipient": [ALICE], "targetTimestamp": [1000, 1001], "type": "read"},
        {"recipient": [BOB], "targetTimestamp": [1002, 1003], "type": "read"},
        {"recipient": [ALICE], "targetTimestamp": [1004], "type": "read"},
    ]


# ── lifecycle: one runtime, nothing lost on stop, flows cleaned up ──────────────

def _live_tasks(name: str) -> list:
    from backend.tasks import _background_tasks
    return [t for t in _background_tasks if not t.done() and t.get_name() == name]


@pytest.mark.asyncio
async def test_concurrent_starts_run_one_signal_cli_that_stop_ends(db, fake_cli, events):
    _linked()
    _scenario(contacts=[], groups=[])
    acc = await _account(db)
    conn = SignalConnector(acc.id)
    assert await asyncio.gather(conn.start(), conn.start()) == [True, True]
    await _until(lambda: _async(conn.status().state == "ready"))
    proc = conn._rpc._proc
    await conn.stop()
    assert (cli.signal_dir() / "fake_runs").read_text() == "1"  # a single jsonRpc child
    assert proc.returncode is not None and _live_tasks(f"signal_supervise:{conn.account_key}") == []


@pytest.mark.asyncio
async def test_stop_while_starting_leaves_nothing_running(db, fake_cli, events):
    _linked()
    _scenario(contacts=[], groups=[])
    acc = await _account(db)
    conn = SignalConnector(acc.id)
    starting = asyncio.ensure_future(conn.start())
    await asyncio.sleep(0)
    await conn.stop()
    assert await starting is False
    assert _live_tasks(f"signal_supervise:{conn.account_key}") == [] and conn.status().state == "stopped"
    assert not (cli.signal_dir() / "fake_runs").exists()


@pytest.mark.asyncio
async def test_stop_stores_envelopes_already_received(db, fake_cli, events, monkeypatch):
    """signal-cli has acknowledged what it handed over and a linked device
    has no history: a stop must not drop queued envelopes."""
    conn = await _running(db, contacts=[], groups=[])
    handle = conn.handle_receive

    async def slow(params):
        await asyncio.sleep(0.1)
        await handle(params)

    monkeypatch.setattr(conn, "handle_receive", slow)
    for i in range(5):
        await conn._on_notification("receive", {"envelope": _env(1000 + i, dataMessage={
            "timestamp": 1000 + i, "message": f"m{i}"})})
    await asyncio.sleep(0.15)
    await conn.stop()
    assert [m.text for m in await _messages()] == [f"m{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried_not_dropped(db, fake_cli, events, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from backend.connectors.signal import connector as conn_mod

    conn = await _running(db, contacts=[], groups=[])
    monkeypatch.setattr(conn, "retry_delays", (0.01, 0.01, 0.01))
    real = conn_mod.ingest_service.ingest
    calls = []

    async def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("INSERT", {}, Exception("database is locked"))
        return await real(*a, **kw)

    monkeypatch.setattr(conn_mod.ingest_service, "ingest", flaky)
    try:
        await conn._on_notification("receive", {"envelope": _env(1000, dataMessage={"timestamp": 1000, "message": "hi"})})
        [msg] = await _until(lambda: _msgs_when(1), timeout=3)
    finally:
        await conn.stop()
    assert msg.text == "hi" and len(calls) == 2


@pytest.mark.asyncio
async def test_a_group_lookup_timeout_keeps_the_message(db, fake_cli, events):
    conn = await _running(db, contacts=[], groups=[])

    async def slow_call(method, params=None, timeout=None):
        raise asyncio.TimeoutError

    conn._rpc.call = slow_call
    try:
        await conn.handle_receive({"envelope": _env(1000, dataMessage={
            "timestamp": 1000, "message": "hello group", "groupInfo": {"groupId": GROUP, "type": "DELIVER"}})})
    finally:
        await conn.stop()
    [msg] = await _messages()
    assert msg.text == "hello group"


async def _exited(proc, timeout=5.0) -> bool:
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False
    return True


def _link_proc(manager, flow_id):
    return manager._flows[flow_id]._flows[flow_id].proc


@pytest.mark.asyncio
async def test_abandoned_link_flows_are_ended_on_eviction_and_stop_all(db, fake_cli, events, monkeypatch):
    from backend.connectors import manager as mgr_mod

    _scenario(link_delay=30)
    manager = ConnectorManager()
    register(manager)
    monkeypatch.setattr(mgr_mod, "MAX_OPEN_FLOWS", 1)
    first = await manager.begin_auth("signal", {})
    first_proc = _link_proc(manager, first.flow_id)
    second = await manager.begin_auth("signal", {})  # evicts the first, never polled again
    second_proc = _link_proc(manager, second.flow_id)
    assert await _exited(first_proc)
    await manager.stop_all()
    assert await _exited(second_proc)


@pytest.mark.asyncio
async def test_unscanned_link_code_expires_without_polling(db, fake_cli, events, monkeypatch):
    from backend.connectors.signal import connector as conn_mod

    _scenario(link_delay=30)
    monkeypatch.setattr(conn_mod, "LINK_TIMEOUT", 0.5)
    conn = SignalConnector()
    step = await conn.begin_auth({})
    assert step.kind == "qr"
    assert await _exited(conn._flows[step.flow_id].proc)
    done = await conn.continue_auth(step.flow_id, {})
    assert done.kind == "error" and "expired" in done.message


@pytest.mark.asyncio
async def test_cancelled_sign_in_ends_the_link_process(db, fake_cli, events):
    _scenario(link_delay=30)
    manager = ConnectorManager()
    register(manager)
    step = await manager.begin_auth("signal", {})
    proc = _link_proc(manager, step.flow_id)
    await manager.cancel_auth(step.flow_id)
    assert await _exited(proc)
    await manager.cancel_auth(step.flow_id)  # unknown / finished: a no-op
    await manager.cancel_auth("nope")
    from backend.connectors.base import AuthFlowError
    with pytest.raises(AuthFlowError):
        await manager.continue_auth(step.flow_id, {})
