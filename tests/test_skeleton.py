def test_skeleton_imports() -> None:
    from lead_scoring import models

    assert models.Permission.SCORE.value == "score"
