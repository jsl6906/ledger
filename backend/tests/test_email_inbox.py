from email.message import EmailMessage

from sqlalchemy import select

from ledger.config import get_settings
from ledger.db.engine import get_sessionmaker
from ledger.models import Attachment, ImportBatch, InboundEmail, Statement
from ledger.statements import email_inbox

ME = "me@example.com"


def _settings(monkeypatch):
    settings = get_settings().model_copy(update={"mail_allowed_senders": f"{ME}, Spouse@Example.com"})
    monkeypatch.setattr(email_inbox, "get_settings", lambda: settings)


def _msg(sender=ME, subject="Fwd: Your bill", auth="mx.example.com; dkim=pass; spf=pass; dmarc=pass") -> EmailMessage:
    m = EmailMessage()
    m["From"] = f"Me <{sender}>"
    m["To"] = "ledger@example.com"
    m["Subject"] = subject
    if auth:
        m["Authentication-Results"] = auth
    m.set_content("See attached.")
    return m


async def _file(m: EmailMessage) -> list[str]:
    async with get_sessionmaker()() as s:
        made = await email_inbox.file_message(s, m.as_bytes())
        await s.commit()
    return made


async def test_attachments_are_filed_and_logos_skipped(monkeypatch):
    _settings(monkeypatch)
    m = _msg()
    m.add_attachment(b"%PDF-1.7 email bill", maintype="application", subtype="pdf", filename="PowerCo-2026-09.pdf")
    csv = b"date,description,amount\n2026-09-01,Coffee,-4.50\n"
    m.add_attachment(csv, maintype="text", subtype="csv", filename="card.csv")
    m.add_attachment(b"\x89PNG tiny logo", maintype="image", subtype="png", filename="logo.png")

    made = await _file(m)
    assert [x.split()[0] for x in made] == ["statement", "import"]
    async with get_sessionmaker()() as s:
        st = await s.get(Statement, int(made[0].split()[1]))
        assert st.status == "processing" and st.job_id
        batch = await s.get(ImportBatch, int(made[1].split()[1]))
        assert batch.origin == "email" and batch.status == "mapping"
        assert await s.scalar(select(Attachment.id).where(Attachment.filename == "logo.png")) is None

    assert await _file(m) == []  # forwarding the same email again files nothing new


async def test_body_only_email_becomes_text_statement(monkeypatch):
    _settings(monkeypatch)
    m = _msg(sender="spouse@example.com", subject="Fwd: Order #123.45 confirmed")
    m.set_content("Plain fallback")
    m.add_alternative(
        "<html><head><style>p{color:red}</style></head><body><p>Thanks for your order</p>"
        "<table><tr><td>Total</td><td>$42.10</td></tr></table><script>x()</script></body></html>",
        subtype="html",
    )

    made = await _file(m)
    assert len(made) == 1 and made[0].startswith("statement")
    async with get_sessionmaker()() as s:
        st = await s.get(Statement, int(made[0].split()[1]))
        att = (
            await s.execute(
                select(Attachment.filename, Attachment.mime_type, Attachment.content).where(
                    Attachment.id == st.attachment_id
                )
            )
        ).one()
    assert att.filename == "Email - Fwd Order #123 45 confirmed" and att.mime_type == "text/plain"
    text = att.content.decode()
    assert "Thanks for your order\nTotal $42.10" in text
    assert "color" not in text and "x()" not in text and "Plain fallback" not in text


async def test_untrusted_mail_is_ignored(monkeypatch):
    _settings(monkeypatch)
    stranger = _msg(sender="someone@evil.example")
    stranger.add_attachment(b"%PDF-1.7 stranger", maintype="application", subtype="pdf", filename="a.pdf")
    spoofed = _msg(auth="mx.example.com; spf=fail; dmarc=fail")
    spoofed.add_attachment(b"%PDF-1.7 spoofed", maintype="application", subtype="pdf", filename="b.pdf")

    assert await _file(stranger) is None
    assert await _file(spoofed) == []


def test_inbox_needs_allowed_senders(monkeypatch):
    settings = get_settings().model_copy(
        update={"imap_host": "imap.example.com", "imap_username": "u", "mail_allowed_senders": None}
    )
    monkeypatch.setattr(email_inbox, "get_settings", lambda: settings)
    assert not email_inbox.inbox_configured()


def test_missing_imap_folder_is_reported(monkeypatch):
    import pytest

    settings = get_settings().model_copy(update={"imap_folder": "Ledger"})
    monkeypatch.setattr(email_inbox, "get_settings", lambda: settings)

    class Conn:
        def select(self, mailbox, readonly=False):
            return "NO", [b"[NONEXISTENT] Unknown Mailbox: Ledger (Failure)"]

        def uid(self, *args):
            raise AssertionError("searched without a selected folder")

        def logout(self):
            pass

    monkeypatch.setattr(email_inbox, "_connect", Conn)
    with pytest.raises(RuntimeError, match="IMAP_FOLDER 'Ledger' could not be opened"):
        email_inbox._find_mail({ME})


class FakeImap:
    """Just enough of an IMAP mailbox for poll_inbox: SEARCH by UNSEEN/SINCE+FROM, FETCH and STORE \\Seen."""

    def __init__(self, messages: dict[int, tuple[EmailMessage, bool]]):
        self.messages = {uid: (m.as_bytes(), seen) for uid, (m, seen) in messages.items()}
        self.fetched: list[int] = []

    def __call__(self):
        return self

    def select(self, mailbox, readonly=False):
        return "OK", [str(len(self.messages)).encode()]

    def logout(self):
        pass

    def uid(self, command, *args):
        if command == "SEARCH":
            sender = args[args.index("FROM") + 1].strip('"')
            hits = [
                uid
                for uid, (raw, seen) in self.messages.items()
                if sender.encode() in raw.split(b"\n\n")[0]
                and ("UNSEEN" not in args or not seen)
                and ("SINCE" not in args or b"me+ledger@example.com" in raw)
            ]
            return "OK", [" ".join(map(str, hits)).encode()]
        if command == "FETCH":
            out = []
            for uid in map(int, args[0].split(",")):
                raw = self.messages[uid][0]
                if "HEADER.FIELDS" in args[1]:
                    raw = b"\r\n".join(x for x in raw.split(b"\n") if x.lower().startswith((b"message-id", b"subject")))
                else:
                    self.fetched.append(uid)
                out += [(f"1 (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
            return "OK", out
        if command == "STORE":
            for uid in map(int, args[0].split(",")):
                self.messages[uid] = (self.messages[uid][0], True)
            return "OK", []
        raise AssertionError(command)


async def test_poll_files_read_mail_sent_to_the_inbound_address_once(monkeypatch):
    settings = get_settings().model_copy(
        update={
            "imap_host": "imap.example.com",
            "imap_username": ME,
            "imap_password": "x",
            "mail_allowed_senders": f"{ME}, spouse@example.com",
            "mail_inbound_address": "me+ledger@example.com",
        }
    )
    monkeypatch.setattr(email_inbox, "get_settings", lambda: settings)
    monkeypatch.setattr(email_inbox, "replies_saved", lambda: False)
    monkeypatch.setattr(email_inbox, "notify_worker", lambda: None)

    forwarded = _msg(subject="Fwd: Water bill", auth=None)  # sent from this mailbox: Gmail stores it as read
    forwarded.replace_header("To", "me+ledger@example.com")
    forwarded["Message-ID"] = "<poll-forward@example.com>"
    forwarded.add_attachment(b"%PDF-1.7 poll water bill", maintype="application", subtype="pdf", filename="water.pdf")
    elsewhere = _msg(subject="Note to self", auth=None)  # read mail from me that wasn't sent to the inbound address
    elsewhere["Message-ID"] = "<poll-elsewhere@example.com>"
    spouse = _msg(sender="spouse@example.com", subject="Fwd: Order poll-42 confirmed")
    spouse["Message-ID"] = "<poll-spouse@example.com>"
    spouse.set_content("Order total $12.00 (poll test)")
    imap = FakeImap({10: (forwarded, True), 11: (elsewhere, True), 12: (spouse, False)})
    monkeypatch.setattr(email_inbox, "_connect", imap)

    assert await email_inbox.poll_inbox() == 2
    assert sorted(imap.fetched) == [10, 12]
    assert all(seen for _, seen in imap.messages.values())
    async with get_sessionmaker()() as s:
        keys = set((await s.scalars(select(InboundEmail.message_key))).all())
    assert {"<poll-forward@example.com>", "<poll-spouse@example.com>"} <= keys

    imap.fetched.clear()
    assert await email_inbox.poll_inbox() == 0  # the forward is still found by date but is not fetched or filed again
    assert imap.fetched == []


def test_message_key_without_message_id_is_stable():
    headers = b"Subject: Hello\r\nDate: Sun, 18 Oct 2026 13:51:30 -0400\r\n\r\n"
    assert email_inbox.message_key(b"Message-ID: <a@b>\r\n\r\n") == "<a@b>"
    assert email_inbox.message_key(headers) == email_inbox.message_key(headers)
    assert email_inbox.message_key(headers) != email_inbox.message_key(headers.replace(b"Hello", b"Bye"))
    assert email_inbox.message_key(headers).startswith("sha256:")


async def test_question_reply_is_saved_on_transactions(client, monkeypatch):
    from ledger.alerts import questions

    _settings(monkeypatch)
    sent: list[dict] = []

    async def fake_send(to, subject, text, html=None, reply_to=None, message_id=None):
        sent.append({"subject": subject, "text": text, "reply_to": reply_to, "message_id": message_id})

    monkeypatch.setattr(questions, "send_email", fake_send)
    monkeypatch.setattr(questions, "smtp_configured", lambda: True)
    monkeypatch.setattr(questions, "replies_saved", lambda: True)
    monkeypatch.setattr(
        questions,
        "get_settings",
        lambda: get_settings().model_copy(update={"mail_inbound_address": "me+ledger@example.com"}),
    )
    await client.post("/api/members", json={"name": "RQ Sam", "initials": "RQS", "email": "rq-sam@example.com"})
    await client.post("/api/alerts/recipients", json={"email": ME, "name": "Me"})
    ids = []
    for d in ("RQ HARDWARE", "RQ HARDWARE 2"):
        t = await client.post("/api/transactions", json={"txn_date": "2031-03-01", "description": d, "amount": "-9.99"})
        ids.append(t.json()["id"])
    r = await client.post(
        "/api/alerts/questions",
        json={
            "transaction_ids": ids,
            "to": ["rq-sam@example.com"],
            "message": "What was this?",
            "subject": "RQ hardware question",
            "reply_to": ME,
        },
    )
    assert r.status_code == 200, r.text
    [q] = sent
    assert q["reply_to"] == f"me+ledger@example.com, {ME}" and "saved on the transactions in Ledger" in q["text"]

    reply = _msg(sender="rq-sam@example.com", subject="Re: " + q["subject"])
    reply["In-Reply-To"] = q["message_id"]
    reply.set_content(
        "Paint for the fence.\n\nSent from my iPhone\n\n"
        "On Fri, Oct 10, 2026 at 9:15 AM Ledger <\nme@example.com> wrote:\n> What was this?\n"
    )
    receipt = b"\xff\xd8 receipt photo" + b"x" * 30_000
    reply.add_attachment(receipt, maintype="image", subtype="jpeg", filename="IMG_1.jpeg")
    made = await _file(reply)
    assert made[0] == "reply on 2 transaction(s)" and made[1].startswith("statement")
    for i in ids:
        notes = (await client.get(f"/api/transactions/{i}/notes")).json()
        assert ("reply", "RQ Sam replied: Paint for the fence.") in [(n["source"], n["body"]) for n in notes]
    st = (await client.get(f"/api/statements/{made[1].split()[1]}")).json()
    assert st["suggestion"]["preset_transaction_ids"] == ids

    assert await _file(reply) == []  # reprocessing the same reply adds nothing

    # A client that dropped the threading headers is matched by subject; strangers are still ignored.
    by_subject = _msg(sender="rq-sam@example.com", subject="RE: " + q["subject"])
    by_subject.set_content("Also primer.")
    assert await _file(by_subject) == ["reply on 2 transaction(s)"]
    stranger = _msg(sender="x@evil.example", subject="Re: " + q["subject"])
    assert await _file(stranger) is None
