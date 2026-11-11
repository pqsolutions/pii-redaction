"""
Email (.eml / .msg) read + redact-in-place helpers, shared by
dual_redaction_pipeline.py (batch redaction) and redaction_formats.py
(preview / rehydration / single-value edits).

Scope (per the "Body + headers" decision): the Subject/From/To/Cc headers and
the text/plain + text/html body parts are redacted. Attachments inside the
email are left untouched.

.eml  — handled with the Python stdlib `email` package (read + write), no
        external dependency.
.msg  — Outlook (.msg) is an OLE compound file with no pure-Python writer, so
        read + write both go through Outlook via COM (win32com). Requires
        Microsoft Outlook to be installed wherever this runs — same Office-COM
        requirement the .doc path already has (convert_doc_to_docx()).

Every text-touching function takes a `transform_fn(text) -> (new_text, events)`
closure so the two callers can plug in their own masking/unmasking logic
(apply_masking() with its secret_map/candidate_counters in the pipeline, the
plain {value: token} substitution in redaction_formats) without this module
needing to know about either.
"""
import os
from email import message_from_binary_file
from email.generator import BytesGenerator
from email.headerregistry import HeaderRegistry, UnstructuredHeader
from email.message import EmailMessage
from email.policy import default as _default_policy
from pathlib import Path

# .msg handling. Outlook COM gives true in-place .msg output but needs Outlook
# installed, an interactive session, and its "programmatic access" security
# guard disabled — otherwise it blocks/hangs. So the DEFAULT, reliable path
# reads the .msg with the pure-Python `extract-msg` library and writes a
# redacted .eml (no Outlook, cannot hang). Set PII_MSG_USE_OUTLOOK=1 to prefer
# Outlook when it is properly configured; it still falls back to .eml on error.
MSG_USE_OUTLOOK = os.environ.get("PII_MSG_USE_OUTLOOK", "").strip().lower() in ("1", "true", "yes", "on")

# policy.default parses From/To/Cc/etc. as structured ADDRESS headers, which
# strips the angle brackets off a redaction token like "<*EMAIL_1*>" (turning
# it into display-name "*EMAIL_1*" + empty address "<>") and would break
# rehydration, since the exact token must survive to be found and restored.
# Mapping just those headers to UnstructuredHeader keeps their values as plain
# strings (tokens pass through verbatim) while Content-Type etc. stay structured
# so set_content() still re-encodes bodies correctly.
_addr_registry = HeaderRegistry()
for _h in ("from", "to", "cc", "bcc", "sender", "reply-to", "resent-from",
           "resent-to", "resent-cc"):
    _addr_registry.map_to_type(_h, UnstructuredHeader)
_EML_POLICY = _default_policy.clone(header_factory=_addr_registry)

# Headers that carry PII we redact. From/To/Cc hold addresses + display names;
# Subject is free text.
EMAIL_HEADERS = ("Subject", "From", "To", "Cc")
_TEXT_TYPES = ("text/plain", "text/html")

# Outlook SaveAs format + OpenSharedItem close mode.
_OL_MSG_UNICODE = 9   # olMSGUnicode
_OL_DISCARD = 1       # olDiscard — close without saving changes back to source


# ----------------------------------------------------------------------
# .eml  (stdlib email, policy.default / EmailMessage)
# ----------------------------------------------------------------------

def _load_eml(path: Path):
    with open(path, "rb") as f:
        return message_from_binary_file(f, policy=_EML_POLICY)


def _redactable_text_parts(msg):
    """Yield the body parts we mask: non-multipart text/plain or text/html that
    are not attachments."""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        cd = (part.get_content_disposition() or "").lower()
        if cd == "attachment":
            continue
        if part.get_content_type() in _TEXT_TYPES:
            yield part


def extract_eml_text(path: Path) -> list:
    """All redactable text in an .eml — the four headers plus every text body
    part — as a list of strings. Used for Phase 1 detection and token scans."""
    msg = _load_eml(path)
    out = []
    for h in EMAIL_HEADERS:
        v = msg.get(h)
        if v:
            out.append(str(v))
    for part in _redactable_text_parts(msg):
        try:
            content = part.get_content()
        except Exception:
            continue
        if content:
            out.append(str(content))
    return out


def _html_to_text(html: str) -> str:
    """Lightweight HTML -> readable text for previews: block tags become line
    breaks, everything else is stripped, entities are unescaped. Not a full
    renderer — just enough to show an email body as lines instead of one long
    raw-HTML string."""
    import html as _htmllib
    import re
    s = html or ""
    s = re.sub(r"(?is)<\s*(script|style|head)[^>]*>.*?<\s*/\s*\1\s*>", "", s)
    s = re.sub(r"(?i)<\s*br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</\s*(p|div|li|tr|h[1-6]|ul|ol|table|blockquote)\s*>", "\n", s)
    s = re.sub(r"(?i)<\s*(p|div|li|tr|h[1-6])[^>]*>", "\n", s)
    # Strip remaining tags, but NOT redaction tokens like "<*PERSON_1*>" which
    # also start with '<' and end with '>' — the (?!\*) lookahead spares them.
    s = re.sub(r"<(?!\*)[^>]+>", "", s)
    s = _htmllib.unescape(s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def email_preview_lines(path: Path) -> list:
    """Readable, line-by-line view of an email for the on-screen preview:
    labelled headers, a blank separator, then the body as text (plain-text part
    preferred; the HTML part is converted to text when that's all there is).
    Unlike extract_email_text() — which returns raw strings for detection — this
    renders the HTML body into lines instead of one long tag soup string."""
    ext = Path(path).suffix.lower()
    lines = []

    if ext == ".eml":
        msg = _load_eml(path)
        for h in EMAIL_HEADERS:
            v = msg.get(h)
            if v:
                lines.append(f"{h}: {v}")
        plain = html = None
        for part in _redactable_text_parts(msg):
            try:
                content = part.get_content()
            except Exception:
                continue
            if not content or not str(content).strip():
                continue
            if part.get_content_type() == "text/plain" and plain is None:
                plain = str(content)
            elif part.get_content_type() == "text/html" and html is None:
                html = str(content)
    elif ext == ".msg":
        # Only hit if a raw .msg is previewed (redacted output is normally .eml).
        import extract_msg
        m = extract_msg.Message(str(Path(path).resolve()))
        try:
            for label, v in (("Subject", m.subject), ("From", m.sender),
                             ("To", m.to), ("Cc", m.cc)):
                if v:
                    lines.append(f"{label}: {v}")
            plain = str(m.body) if m.body else None
            html = _decode_html(getattr(m, "htmlBody", None))
        finally:
            try:
                m.close()
            except Exception:
                pass
    else:
        return [str(c) for chunk in extract_email_text(path) for c in str(chunk).splitlines()]

    body = plain if (plain and plain.strip()) else (_html_to_text(html) if html else "")
    if lines and body:
        lines.append("")  # blank separator between headers and body
    lines.extend(body.splitlines())
    return lines


def rewrite_eml(in_path: Path, out_path: Path, transform_fn) -> list:
    """Mask the four headers + every text body part in place and write the
    result as a .eml. transform_fn(text) -> (new_text, events). Returns the
    combined events list (empty if nothing changed)."""
    msg = _load_eml(in_path)
    events = []

    for h in EMAIL_HEADERS:
        v = msg.get(h)
        if v is None:
            continue
        old = str(v)
        new, evs = transform_fn(old)
        if evs and new != old:
            # del+assign avoids the structured address parser choking on a
            # token like "<*EMAIL_1*>" the way replace_header on an address
            # header can; a plain string header round-trips cleanly.
            del msg[h]
            msg[h] = new
            events.extend(evs)

    for part in _redactable_text_parts(msg):
        try:
            content = part.get_content()
        except Exception:
            continue
        if not content:
            continue
        old = str(content)
        new, evs = transform_fn(old)
        if evs and new != old:
            # set_content re-derives Content-Transfer-Encoding/charset, so an
            # originally base64/quoted-printable part stays valid after edit.
            part.set_content(new, subtype=part.get_content_subtype())
            events.extend(evs)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        BytesGenerator(f, policy=msg.policy).flatten(msg)
    return events


# ----------------------------------------------------------------------
# .msg  (Outlook via COM)
# ----------------------------------------------------------------------

# MailItem fields we read for detection. Sender* are read-only on a received
# item, so they can be scanned but not rewritten (noted in rewrite_msg()).
_MSG_READ_ATTRS = ("Subject", "SenderName", "SenderEmailAddress", "To", "CC", "Body")
# Fields we attempt to rewrite. Body + HTMLBody cover the message text in both
# representations; To/CC/Subject are writable display strings.
_MSG_WRITE_ATTRS = ("Subject", "To", "CC", "Body", "HTMLBody")


def _open_outlook(retries: int = 8, delay: float = 2.0):
    """Get an Outlook.Application COM object, retrying the common cold-start
    errors. On a fresh launch Outlook often rejects the first COM calls with
    RPC_E_CALL_REJECTED (-2147418111, "Call was rejected by callee") or
    "Operation unavailable" (-2147221021) until it finishes starting, so we
    back off and retry rather than failing the whole redaction.

    Late-bound (dynamic) dispatch avoids the gen_py cache corruption that breaks
    gencache.EnsureDispatch('Outlook.Application') on some boxes."""
    import time as _time
    import win32com.client as _wc
    _TRANSIENT = (-2147418111, -2147221021, -2147417846)  # call rejected / unavailable / busy
    last = None
    for attempt in range(retries):
        try:
            return _wc.Dispatch("Outlook.Application")
        except Exception as e:
            last = e
            hr = getattr(e, "hresult", None) or (e.args[0] if getattr(e, "args", None) else None)
            if hr in _TRANSIENT or attempt < retries - 1:
                _time.sleep(delay)
                continue
            raise
    raise last


def extract_msg_text(path: Path) -> list:
    """Redactable text from a .msg via Outlook COM: subject, sender, to/cc, and
    body. Used for Phase 1 detection and token scans."""
    import pythoncom
    pythoncom.CoInitialize()
    item = None
    try:
        app = _open_outlook()
        item = app.Session.OpenSharedItem(str(Path(path).resolve()))
        out = []
        for attr in _MSG_READ_ATTRS:
            try:
                v = getattr(item, attr)
            except Exception:
                continue
            if v:
                out.append(str(v))
        return out
    finally:
        if item is not None:
            try:
                item.Close(_OL_DISCARD)
            except Exception:
                pass
        pythoncom.CoUninitialize()


def rewrite_msg(in_path: Path, out_path: Path, transform_fn) -> list:
    """Mask Subject/To/CC/Body/HTMLBody of a .msg via Outlook COM and SaveAs a
    new .msg. transform_fn(text) -> (new_text, events). Returns the combined
    events list. SenderName/SenderEmailAddress are read-only on a received item
    and are left as-is (Outlook does not allow rewriting them)."""
    import pythoncom
    pythoncom.CoInitialize()
    item = None
    try:
        app = _open_outlook()
        item = app.Session.OpenSharedItem(str(Path(in_path).resolve()))
        events = []
        for attr in _MSG_WRITE_ATTRS:
            try:
                v = getattr(item, attr)
            except Exception:
                continue
            if not v:
                continue
            old = str(v)
            new, evs = transform_fn(old)
            if evs and new != old:
                try:
                    setattr(item, attr, new)
                    events.extend(evs)
                except Exception as e:
                    print(f"    [WARN] .msg field '{attr}' is read-only, left unredacted: {e}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        item.SaveAs(str(Path(out_path).resolve()), _OL_MSG_UNICODE)
        return events
    finally:
        if item is not None:
            try:
                item.Close(_OL_DISCARD)
            except Exception:
                pass
        pythoncom.CoUninitialize()


# ----------------------------------------------------------------------
# .msg  (pure-Python via extract-msg — no Outlook; converts to .eml)
# ----------------------------------------------------------------------

def _decode_html(html):
    if html is None:
        return None
    if isinstance(html, bytes):
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                return html.decode(enc)
            except Exception:
                continue
        return html.decode("utf-8", errors="replace")
    return str(html)


def msg_to_eml(msg_path: Path, eml_path: Path):
    """Convert a .msg to a .eml using the pure-Python extract-msg library, so
    the rest of the pipeline can treat it exactly like any other email. Carries
    over Subject/From/To/Cc/Date and the plain-text + HTML bodies. Attachments
    are not carried over (out of scope — Body+headers only)."""
    import extract_msg
    m = extract_msg.Message(str(Path(msg_path).resolve()))
    try:
        em = EmailMessage(policy=_EML_POLICY)
        for header, value in (("Subject", m.subject), ("From", m.sender),
                              ("To", m.to), ("Cc", m.cc)):
            if value:
                em[header] = str(value)
        try:
            if getattr(m, "date", None):
                em["Date"] = str(m.date)
        except Exception:
            pass
        body = m.body or ""
        html = _decode_html(getattr(m, "htmlBody", None))
        # Many .msg files carry only an HTML body (empty plain text). Give the
        # text/plain part a readable text rendering of the HTML so mail clients
        # that show the plain alternative don't fall back to raw HTML source.
        if (not body or not str(body).strip()) and html:
            body = _html_to_text(html)
        em.set_content(str(body) if body else "")
        if html:
            em.add_alternative(html, subtype="html")
    finally:
        try:
            m.close()
        except Exception:
            pass
    eml_path.parent.mkdir(parents=True, exist_ok=True)
    with open(eml_path, "wb") as f:
        BytesGenerator(f, policy=em.policy).flatten(em)


def extract_msg_text_py(path: Path) -> list:
    """Redactable text from a .msg via extract-msg (no Outlook): subject,
    sender, to/cc, and the plain + HTML bodies."""
    import extract_msg
    m = extract_msg.Message(str(Path(path).resolve()))
    try:
        out = []
        for v in (m.subject, m.sender, m.to, m.cc, m.body):
            if v:
                out.append(str(v))
        html = _decode_html(getattr(m, "htmlBody", None))
        if html:
            out.append(html)
        return out
    finally:
        try:
            m.close()
        except Exception:
            pass


# ----------------------------------------------------------------------
# Extension-keyed convenience wrappers
# ----------------------------------------------------------------------

EMAIL_EXTENSIONS = {".eml", ".msg"}


def redacted_output_ext(input_ext: str) -> str:
    """Extension of the redacted output for a given email input. .eml stays
    .eml; a .msg redacts to .eml unless Outlook mode is enabled (then .msg)."""
    input_ext = input_ext.lower()
    if input_ext == ".msg" and not MSG_USE_OUTLOOK:
        return ".eml"
    return input_ext


def extract_email_text(path: Path) -> list:
    ext = Path(path).suffix.lower()
    if ext == ".eml":
        return extract_eml_text(path)
    if ext == ".msg":
        if MSG_USE_OUTLOOK:
            try:
                return extract_msg_text(path)
            except Exception as e:
                print(f"    [WARN] Outlook read of {Path(path).name} failed ({e}); using extract-msg")
        return extract_msg_text_py(path)
    raise ValueError(f"Not an email file: {ext}")


def rewrite_email(in_path: Path, out_path: Path, transform_fn) -> list:
    """Redact an email. .eml is rewritten directly. A .msg is converted to .eml
    first (pure-Python) and the redacted output is a .eml — callers pass an
    .eml out_path for .msg input (see redacted_output_ext()). With
    PII_MSG_USE_OUTLOOK=1 a .msg is instead redacted in place via Outlook COM,
    falling back to the .eml path on any error."""
    ext = Path(in_path).suffix.lower()
    if ext == ".eml":
        return rewrite_eml(in_path, out_path, transform_fn)
    if ext == ".msg":
        if MSG_USE_OUTLOOK:
            try:
                return rewrite_msg(in_path, out_path, transform_fn)
            except Exception as e:
                print(f"    [WARN] Outlook redaction of {Path(in_path).name} failed "
                      f"({e}); falling back to .eml output")
        tmp_eml = Path(out_path).with_name(Path(out_path).stem + "__srcmsg.eml")
        try:
            msg_to_eml(in_path, tmp_eml)
            return rewrite_eml(tmp_eml, out_path, transform_fn)
        finally:
            try:
                tmp_eml.unlink(missing_ok=True)
            except Exception:
                pass
    raise ValueError(f"Not an email file: {ext}")
