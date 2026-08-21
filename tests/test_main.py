import pytest

from sqlassert import analyze


def test_optional_analysis_arguments_are_keyword_only():
    with pytest.raises(TypeError):
        analyze("SELECT 1", None)
