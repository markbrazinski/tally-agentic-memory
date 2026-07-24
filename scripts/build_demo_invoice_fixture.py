"""Build the deterministic fictional invoice used by the locked Intake demo."""

from __future__ import annotations

from pathlib import Path

OUTPUT = Path(__file__).parents[1] / "tests/fixtures/demo/INV-1048.pdf"


def _escape_pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf() -> bytes:
    lines = (
        (20, "FICTIONAL DEMO INVOICE - NOT A REAL CARRIER CHARGE"),
        (16, "Asterline Demo Shipping"),
        (12, "Invoice: INV-1048"),
        (12, "Issued: June 22, 2026"),
        (12, "Bill of Lading: OAK-77421"),
        (12, "Container: TLLU-482931-7"),
        (12, "Charge Type: Demurrage"),
        (12, "Charge Period: June 8, 2026 through June 14, 2026"),
        (12, "Charged Days: 7"),
        (12, "Daily Rate: USD $350.00 per day"),
        (14, "Total Amount Due: USD $2,450.00"),
        (10, "Synthetic hackathon fixture. No carrier was contacted."),
    )
    commands = ["BT", "/F1 20 Tf", "72 742 Td"]
    for index, (size, line) in enumerate(lines):
        if index:
            commands.extend(("0 -34 Td", f"/F1 {size} Tf"))
        commands.append(f"({_escape_pdf_text(line)}) Tj")
    commands.append("ET")
    stream = ("\n".join(commands) + "\n").encode("ascii")

    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
        + stream
        + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    )

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


if __name__ == "__main__":
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(build_pdf())
    print(f"wrote {OUTPUT}")
