from __future__ import annotations

import datetime as _dt
import textwrap
from pathlib import Path


def _pdf_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("\r", "")
    )


def _build_minimal_pdf(pages: list[list[str]], output_path: Path) -> None:
    """
    Minimal PDF writer (no external deps).
    - Built-in Helvetica font
    - Each entry in `pages` is a list[str] of lines
    """

    # A4 in points.
    page_w, page_h = 595.28, 841.89
    left_margin = 40
    top_margin = 40
    font_size = 10
    leading = 13
    max_lines_per_page = int((page_h - (top_margin * 2)) / leading)

    # Normalize: enforce max lines per page.
    normalized_pages: list[list[str]] = []
    current: list[str] = []
    for page in pages:
        for line in page:
            current.append(line)
            if len(current) >= max_lines_per_page:
                normalized_pages.append(current)
                current = []
        if current:
            normalized_pages.append(current)
            current = []
    if not normalized_pages:
        normalized_pages = [[""]]

    objects: list[bytes] = []

    def add_obj(data: str) -> int:
        objects.append(data.encode("utf-8"))
        return len(objects)

    font_obj = add_obj("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_objs: list[int] = []

    for lines in normalized_pages:
        y = page_h - top_margin - font_size
        content_lines = ["BT", f"/F1 {font_size} Tf", f"{left_margin} {y:.2f} Td"]
        for i, line in enumerate(lines):
            if i > 0:
                content_lines.append(f"0 -{leading} Td")
            content_lines.append(f"({_pdf_escape(line)}) Tj")
        content_lines.append("ET")
        stream = "\n".join(content_lines)

        content_obj = add_obj(
            f"<< /Length {len(stream.encode('utf-8'))} >>\nstream\n{stream}\nendstream"
        )

        page_obj = add_obj(
            f"<< /Type /Page /Parent 0 0 R /MediaBox [0 0 {page_w:.2f} {page_h:.2f}] "
            f"/Resources << /Font << /F1 {font_obj} 0 R >> >> "
            f"/Contents {content_obj} 0 R >>"
        )
        page_objs.append(page_obj)

    kids = " ".join([f"{p} 0 R" for p in page_objs])
    pages_obj = add_obj(f"<< /Type /Pages /Kids [ {kids} ] /Count {len(page_objs)} >>")
    catalog_obj = add_obj(f"<< /Type /Catalog /Pages {pages_obj} 0 R >>")

    # Patch in correct parent reference by rewriting page objects.
    for page_obj_num in page_objs:
        raw = objects[page_obj_num - 1].decode("utf-8")
        objects[page_obj_num - 1] = raw.replace(
            "/Parent 0 0 R", f"/Parent {pages_obj} 0 R"
        ).encode("utf-8")

    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets: list[int] = [0]
    body = bytearray()
    body.extend(header)

    for i, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body.extend(f"{i} 0 obj\n".encode("utf-8"))
        body.extend(obj)
        body.extend(b"\nendobj\n")

    xref_start = len(body)
    body.extend(f"xref\n0 {len(objects)+1}\n".encode("utf-8"))
    body.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        body.extend(f"{off:010d} 00000 n \n".encode("utf-8"))

    trailer = (
        f"trailer\n<< /Size {len(objects)+1} /Root {catalog_obj} 0 R >>\n"
        f"startxref\n{xref_start}\n%%EOF\n"
    ).encode("utf-8")
    body.extend(trailer)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(bytes(body))


def build_pdf(output_path: Path) -> None:
    today = _dt.date.today().isoformat()

    def wrap(s: str, width: int = 100) -> list[str]:
        if not s.strip():
            return [""]
        return textwrap.wrap(
            s,
            width=width,
            break_long_words=False,
            replace_whitespace=False,
        )

    def add_title(lines: list[str], text: str) -> None:
        lines.extend(wrap(text.upper(), 100))
        lines.append("")

    def add_section(lines: list[str], title: str) -> None:
        lines.append("=" * 100)
        lines.extend(wrap(title.upper(), 100))
        lines.append("=" * 100)
        lines.append("")

    def add_bullets(lines: list[str], bullets: list[str]) -> None:
        for b in bullets:
            for part in wrap(f"- {b}", 100):
                lines.append(part)
        lines.append("")

    def add_case(
        lines: list[str],
        _id: str,
        title: str,
        pre: str,
        steps: str,
        expected: str,
        priority: str,
    ) -> None:
        lines.append(f"{_id} [{priority}]  {title}")
        for part in wrap(f"Shartein (conditions): {pre}", 100):
            lines.append(f"  {part}")
        for part in wrap(f"Steps (kaise test karein): {steps}", 100):
            lines.append(f"  {part}")
        for part in wrap(f"Expected (kya result hona chahiye): {expected}", 100):
            lines.append(f"  {part}")
        lines.append("")

    lines: list[str] = []
    add_title(lines, "Booking Test Cases (Hindi)")
    lines.extend(wrap(f"Date: {today}", 100))
    lines.extend(
        wrap(
            "Purpose: Booking flow me kya-kya cheezein test karni chahiye (conditions + expected results). "
            "Is document me API endpoint ka mention nahi hai; sirf rules/conditions cover kiye gaye hain: "
            "tenant scope, class status/time rules, payment modes (free/wallet/gateway/cash/package), "
            "capacity/waitlist, aur layout seats.",
            100,
        )
    )
    lines.append("")

    add_section(lines, "Legend / Priority")
    add_bullets(
        lines,
        [
            "P0 = business critical (data integrity / money / capacity).",
            "P1 = important (policy/policies + common edge cases).",
            "P2 = nice-to-have (rare edge cases / UX polish).",
            "Rule: Validation fail ho to booking create nahi honi chahiye; aur side-effects (wallet debit / package sessions) bhi mutate nahi hone chahiye.",
        ],
    )

    add_section(lines, "A) Validation rules (booking se pehle)")
    add_case(
        lines,
        "BKG-V-001",
        "Happy path (basic) — class bookable",
        "Class active/published ho, future start ho, user same tenant ka ho, capacity available ho, aur payment_mode allowed ho.",
        "Normal booking attempt karein (correct payment_mode + required fields).",
        "Booking allowed; status 'confirmed' ya 'pending' (auto-confirm setting ke hisaab se).",
        "P0",
    )
    add_case(
        lines,
        "BKG-V-002",
        "Reject — class already started",
        "Class start_time tenant timezone ke hisaab se already start ho chuka ho.",
        "Booking try karein (any payment_mode).",
        "Booking reject; clear message 'Class has already started' type; no booking created.",
        "P0",
    )
    add_case(
        lines,
        "BKG-V-003",
        "Reject — booking cutoff time crossed",
        "Config me booking_cutoff_minutes set ho; ab time cutoff se aage nikal gaya ho (starts_at ke close).",
        "Booking try karein when now > starts_at - cutoff.",
        "Booking reject; message cutoff minutes mention kare; no booking created.",
        "P1",
    )
    add_case(
        lines,
        "BKG-V-004",
        "Reject — payment_mode allowed nahi",
        "Paid class par free select karna; free class par wallet/gateway select karna; package-only class par package ke alawa kuch aur select karna.",
        "Wrong payment_mode ke saath booking try karein.",
        "Booking reject; user ko allowed payment modes ka hint mile; no booking created.",
        "P0",
    )
    add_case(
        lines,
        "BKG-V-005",
        "Reject — duplicate booking (same class)",
        "Same user ke paas already active booking ho: confirmed / waiting / pending / pending_payment.",
        "Same class ko dubara book karne ki koshish karein.",
        "Booking reject; duplicate prevent ho; no extra booking row.",
        "P0",
    )
    add_case(
        lines,
        "BKG-V-006",
        "Capacity full — waitlist allowed case",
        "Capacity reached ho; waiting list enabled ho; max_waitings configured ho aur waiting list full na ho.",
        "Class full hone par booking try karein.",
        "User waitlist me add ho sake; waiting_position set ho (next number).",
        "P0",
    )
    add_case(
        lines,
        "BKG-V-007",
        "Capacity full — waitlist disabled/full → reject",
        "Capacity reached ho; ya to waiting list disabled ho, ya waiting list already full ho.",
        "Booking try karein.",
        "Booking reject; message me 'Class is full' + reason aaye; no booking created.",
        "P0",
    )
    add_case(
        lines,
        "BKG-V-008",
        "Layout class — seat required (when real slot available)",
        "Class me layout configured ho (seats); capacity available ho.",
        "Seat select kiye bina booking try karein.",
        "Booking reject; seat required message; no booking created.",
        "P0",
    )
    add_case(
        lines,
        "BKG-V-009",
        "Layout class — waitlist booking me seat required nahi",
        "Class full ho + waitlist allowed ho + layout configured ho.",
        "Seat select kiye bina waitlist booking try karein.",
        "Waitlist allow ho; seat selection defer ho.",
        "P1",
    )
    add_case(
        lines,
        "BKG-V-010",
        "Reject — seat already taken / unavailable",
        "Layout class ho; chosen seat already booked/unavailable ho, ya kisi active booking ne same seat le rakhi ho.",
        "Taken seat ke saath booking try karein.",
        "Booking reject; message 'seat already taken/not available'.",
        "P0",
    )
    add_case(
        lines,
        "BKG-V-011",
        "Reject — wallet insufficient (paid class)",
        "Paid class ho; user.wallet < price ho; pay-per-class enabled ho.",
        "Wallet payment select karke booking try karein.",
        "Booking reject; clear message 'Insufficient wallet balance'.",
        "P0",
    )
    add_case(
        lines,
        "BKG-V-012",
        "Reject — package booking invalid (sale missing/invalid/sessions 0)",
        "Package mode enabled ho; valid sale id required ho; sessions_remaining >= 1 hona chahiye.",
        "Package payment ke saath (a) missing/invalid sale id, (b) sessions_remaining=0 ke cases try karein.",
        "Booking reject; user ko payment selection/change method ka prompt; no booking created.",
        "P0",
    )
    add_case(
        lines,
        "BKG-V-013",
        "Reject — gender restriction",
        "Class gender restricted (male/female) ho; user gender mismatch ho ya unset ho.",
        "Booking try karein.",
        "Booking reject; restriction message show ho; no booking created.",
        "P1",
    )

    add_section(lines, "B) Booking create (side-effects ke saath)")
    add_case(
        lines,
        "BKG-C-001",
        "Create booking success (confirmed/pending)",
        "Validations pass; capacity available; payment_mode allowed.",
        "Booking create karein.",
        "Booking create ho; status confirmed ya pending; booking_counts expected tarike se update ho.",
        "P0",
    )
    add_case(
        lines,
        "BKG-C-002",
        "Gateway booking — status pending_payment",
        "Gateway mode enabled ho; payment_mode gateway allowed ho; slot available ho.",
        "Gateway payment ke saath booking create karein.",
        "Booking status pending_payment ho; user ko payment flow me le jaye; duplicate booking na bane.",
        "P0",
    )
    add_case(
        lines,
        "BKG-C-003",
        "Wallet booking — upfront wallet debit",
        "Paid class; wallet >= price; payment_mode wallet.",
        "Booking create karein.",
        "Wallet amount price se reduce ho; booking create ho; refund/cancel scenarios ke liye audit marker present ho (notes marker).",
        "P0",
    )
    add_case(
        lines,
        "BKG-C-004",
        "Package booking — session deduct",
        "Valid package sale ho; sessions_remaining>=1; payment_mode package.",
        "Booking create karein.",
        "sessions_remaining 1 se decrease ho; booking me sessions_deducted=1 set ho.",
        "P0",
    )
    add_case(
        lines,
        "BKG-C-005",
        "Reject create — validation fail cases",
        "Any failing case: wrong payment_mode / seat missing / duplicate / capacity full without waitlist etc.",
        "Direct booking create try karein.",
        "Booking create nahi ho; wallet debit / sessions deduct jaise side-effects nahi hone chahiye.",
        "P0",
    )
    add_case(
        lines,
        "BKG-C-006",
        "Layout booking — seat status booked",
        "Layout class; seat_id provided; booking occupying status (confirmed/pending/pending_payment).",
        "Booking create karein.",
        "Seat status 'booked' ho; same seat kisi aur active booking ko na mile.",
        "P0",
    )

    add_section(lines, "C) Waitlist")
    add_case(
        lines,
        "BKG-W-001",
        "Waitlist add (jab class full ho)",
        "Capacity full ho; waitlist enabled ho; max_waitings exceed na ho.",
        "Waitlist booking try karein.",
        "Status waiting set ho; waiting_position correct ho.",
        "P0",
    )
    add_case(
        lines,
        "BKG-W-002",
        "Waitlist reject (jab slot available ho)",
        "Capacity available ho (slot free ho).",
        "Waitlist attempt karein.",
        "Waitlist reject ho; user ko regular booking karne ko bola jaye.",
        "P1",
    )
    add_case(
        lines,
        "BKG-W-003",
        "Waitlist reject — waiting list disabled",
        "Class full ho, lekin waiting list feature disabled ho (gym setting / class policy).",
        "Class full hone par waitlist join karne ki koshish karein.",
        "Reject ho; clear message aaye; waiting booking create na ho.",
        "P0",
    )
    add_case(
        lines,
        "BKG-W-004",
        "Waitlist reject — max_waitings set nahi ya 0",
        "Class full ho; waitlist enabled ho sakti hai, lekin max_waitings 0/empty ho.",
        "Waitlist join try karein.",
        "Reject ho; 'waiting list not configured' type message aaye; waiting booking create na ho.",
        "P1",
    )
    add_case(
        lines,
        "BKG-W-005",
        "Waitlist reject — waitlist full",
        "Class full ho; max_waitings configured ho; already max_waitings members waiting me ho.",
        "New user se waitlist join try karein.",
        "Reject ho; 'waiting list is full' type message; waiting_position increment na ho.",
        "P0",
    )
    add_case(
        lines,
        "BKG-W-006",
        "Waitlist not allowed — one-to-one class (capacity = 1)",
        "One-to-one class ho (capacity=1) aur already kisi aur ka booking occupy kar raha ho.",
        "Second user se booking/waitlist try karein.",
        "Reject ho; one-to-one me waitlist allow na ho; koi waiting booking create na ho.",
        "P0",
    )
    add_case(
        lines,
        "BKG-W-007",
        "Waitlist duplicate prevent — same user already waiting",
        "User already same class ke waitlist me ho (status waiting).",
        "Same user se dobara waitlist join try karein.",
        "Reject ho; duplicate waiting booking create na ho.",
        "P0",
    )
    add_case(
        lines,
        "BKG-W-008",
        "Waitlist order — promotion oldest waiting ko mile",
        "Waitlist me multiple members ho; unka order (booked_at/created_at) different ho.",
        "Confirmed booking cancel karke slot free karein.",
        "Sabse purana waiting user promote ho; baaki users ka order consistent rahe.",
        "P1",
    )
    add_case(
        lines,
        "BKG-W-009",
        "Promotion + gateway — waiting se promote hone par pending_payment",
        "Waitlist me user ka payment_mode gateway ho; slot free ho jaye.",
        "Slot free hote hi (cancel confirmed) promotion observe karein.",
        "Promoted booking ka status pending_payment ho; user ko payment complete karna pade.",
        "P0",
    )
    add_case(
        lines,
        "BKG-W-010",
        "Layout class + waitlist — seat selection defer hoti hai",
        "Layout class full ho; user waitlist me join kare; seat_id na diya ho.",
        "Waitlist join karein without seat selection; phir slot free karke promotion trigger karein.",
        "Waitlist join ho jaye; promotion ke time seat selection/assignment ka expected behavior define ho (no crash, no duplicate seat).",
        "P1",
    )

    add_section(lines, "D) Cancel + promotion (waitlist se slot fill)")
    add_case(
        lines,
        "BKG-X-001",
        "Cancel confirmed → next waiting promote",
        "Same class me (a) ek confirmed booking ho (b) kam se kam 1 waiting booking ho.",
        "Confirmed booking cancel karein.",
        "booking_counts decrement ho; oldest waiting promote ho (confirmed/pending ya gateway case me pending_payment); waiting_position clear ho.",
        "P0",
    )
    add_case(
        lines,
        "BKG-X-002",
        "Package booking cancel → sessions restore",
        "Package booking me sessions_deducted=1 ho aur sale linked ho.",
        "Cancel karein.",
        "sessions_remaining +1 ho; booking cancelled ho.",
        "P0",
    )
    add_case(
        lines,
        "BKG-X-003",
        "Cancel blocked — cutoff ke baad",
        "allow_late_cancellations=false ho; cancellation_window_hours configured ho; cutoff cross ho chuka ho.",
        "Cancel try karein.",
        "Cancel reject ho; policy message mile; booking status unchanged rahe.",
        "P1",
    )
    add_case(
        lines,
        "BKG-X-004",
        "Cancel → seat free (if no one else holds it)",
        "Layout class; booking me seat_id ho; same seat kisi aur active booking me na ho.",
        "Cancel karein.",
        "Seat 'available' ho jaye; seat dubara book ho sake.",
        "P1",
    )
    add_case(
        lines,
        "BKG-X-005",
        "Already cancelled/completed booking ko cancel nahi kar sakte",
        "Booking status cancelled ya completed ho.",
        "Cancel try karein.",
        "Reject ho; clear message mile.",
        "P1",
    )

    add_section(lines, "E) Member bookings list (upcoming/past/waiting)")
    add_case(
        lines,
        "BKG-L-001",
        "Waiting bookings sirf waiting bucket me",
        "Waiting + confirmed/past bookings exist karein.",
        "Bookings list screen/feature me verify karein.",
        "Waiting status wale bookings waiting me; baaki upcoming/past me; can_cancel logic correct.",
        "P1",
    )
    add_case(
        lines,
        "BKG-L-002",
        "Cancel deadline aur can_cancel calculation correct",
        "cancellation_window_hours set ho; class future me ho.",
        "Cancel deadline ko starts_at - window se compare karein; can_cancel behavior verify karein.",
        "cancel_deadline correct ho; allow_late_cancellations/cutoff rules ke saath consistent ho.",
        "P1",
    )

    add_section(lines, "F) Security / Isolation (must-have)")
    add_case(
        lines,
        "BKG-S-001",
        "User A dusre user ki booking cancel/view na kar sake (IDOR)",
        "User A aur User B exist karein; booking B ki ho.",
        "A se B ki booking ko cancel/view try karein.",
        "Access deny (404/403); koi sensitive data leak na ho.",
        "P0",
    )
    add_case(
        lines,
        "BKG-S-002",
        "Cross-tenant class booking blocked (trainer/program mismatch)",
        "Class ka trainer ya training programme kisi aur tenant ka ho.",
        "Current tenant user se booking try karein.",
        "Booking blocked; 'class not in your gym' type failure; booking create na ho.",
        "P0",
    )

    add_section(lines, "Quick execution checklist (fast)")
    add_bullets(
        lines,
        [
            "P0 pehle run karein: payment rules, capacity/waitlist, seat locking, wallet/package side-effects, cancel→promotion.",
            "Har failure me confirm karein: booking create nahi hui, aur wallet debit / package sessions change nahi huye.",
            "Har success me confirm karein: booking_counts, seat status, aur bookings list me correct status.",
        ],
    )

    _build_minimal_pdf([lines], output_path)


if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "Booking_Test_Cases.pdf"
    build_pdf(out)
    print(f"Generated: {out}")

