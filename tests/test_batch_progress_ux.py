"""Batch progress UX — overall line reflects in-progress work."""

from app.blocks import batch_overall_progress_line


def test_overall_shows_in_progress_not_zero_processed():
    rows = [
        {
            "file_label": "a.pdf",
            "stage": "Understanding document",
            "status": "in_progress",
        },
    ]
    line = batch_overall_progress_line(total=1, done=0, doc_rows=rows)
    assert "1 in progress" in line
    assert "0 of 1 documents processed" not in line


def test_overall_shows_complete_when_done():
    rows = [{"file_label": "a.pdf", "stage": "Added", "status": "complete"}]
    line = batch_overall_progress_line(total=1, done=1, doc_rows=rows)
    assert line == "1 of 1 complete"
