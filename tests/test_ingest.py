from copilot.ingest.chunker import split_text
from copilot.ingest.loaders import split_markdown
from copilot.models import Role


def test_manifest_counts(manifest):
    assert manifest["documents"] == 14
    assert set(manifest["by_tenant"]) == {"policy", "engineering"}
    assert set(manifest["by_min_role"]) == {"employee", "manager", "admin"}


def test_section_marker_tightens_access():
    body = (
        "# Doc\n\nopen text\n\n## Secret\n<!-- acl: manager -->\nhidden\n\n## Open again\n\nvisible"
    )
    sections = {s.heading: s.min_role for s in split_markdown(body, Role.EMPLOYEE, "Doc")}
    assert sections["Doc"] == Role.EMPLOYEE
    assert sections["Doc > Secret"] == Role.MANAGER
    assert sections["Doc > Open again"] == Role.EMPLOYEE


def test_section_marker_cannot_loosen_access():
    body = "# Doc\n\n## Section\n<!-- acl: employee -->\ntext"
    (section,) = split_markdown(body, Role.ADMIN, "Doc")
    assert section.min_role == Role.ADMIN


def test_child_sections_inherit_restriction():
    body = "# Doc\n\n## Restricted\n<!-- acl: admin -->\nintro\n\n### Detail\n\nmore"
    sections = {s.heading: s.min_role for s in split_markdown(body, Role.EMPLOYEE, "Doc")}
    assert sections["Doc > Restricted > Detail"] == Role.ADMIN


def test_missing_role_fails_closed(tmp_path):
    from copilot.ingest.loaders import load_markdown

    p = tmp_path / "untagged.md"
    p.write_text("# Untagged\n\nsome text")
    assert load_markdown(p, "policy").min_role == Role.ADMIN


def test_split_text_respects_size():
    text = " ".join(f"Sentence number {i} is here." for i in range(200))
    chunks = split_text(text, size=300, overlap=50)
    assert len(chunks) > 1
    assert all(len(c) <= 300 for c in chunks)


def test_split_text_cuts_long_runs_without_spaces():
    text = "intro " + "x" * 2500 + " outro"
    chunks = split_text(text, 800, 120)
    assert all(len(c) <= 800 for c in chunks)
    assert "".join(chunks).count("x") >= 2500
