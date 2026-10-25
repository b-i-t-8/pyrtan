import pytest

from pyrtan.conditions import ConditionError, holds, validate
from pyrtan.template import MISSING


@pytest.mark.parametrize(
    "cond,value,expected",
    [
        ({"eq": 200}, 200, True),
        ({"eq": 200}, "200", True),
        ({"eq": "story"}, "story", True),
        ({"eq": True}, 1, False),
        ({"ne": 200}, 404, True),
        ({"gte": 500}, 503, True),
        ({"gte": 500}, 499, False),
        ({"lt": 10}, "9", True),
        ({"between": [200, 299]}, 204, True),
        ({"between": [200, 299]}, 300, False),
        ({"matches": "^https://"}, "https://x", True),
        ({"matches": "^https://"}, "http://x", False),
        ({"contains": "err"}, "an error", True),
        ({"contains": 2}, [1, 2], True),
        ({"contains": "k"}, {"k": 1}, True),
        ({"in": [403, 429]}, 429, True),
        ({"in": ["a", "b"]}, "c", False),
        ({"exists": True}, 0, True),
        ({"exists": True}, None, False),
        ({"exists": False}, MISSING, True),
        ({"empty": True}, [], True),
        ({"empty": True}, "", True),
        ({"empty": True}, MISSING, True),
        ({"empty": False}, [1], True),
        ({"type": "list"}, [], True),
        ({"type": "number"}, 4.2, True),
        ({"type": "number"}, True, False),
        ({"type": "int"}, "3", False),
        ({"startswith": "ab"}, "abc", True),
        ({"endswith": "bc"}, "abc", True),
        ({"gte": 1, "lte": 5}, 3, True),
        ({"gte": 1, "lte": 5}, 6, False),
        ({"eq": 1}, MISSING, False),
    ],
)
def test_holds(cond, value, expected):
    assert holds(cond, value) is expected


def test_validate_rejects_unknown_and_bad():
    with pytest.raises(ConditionError):
        validate({"equals": 1})
    with pytest.raises(ConditionError):
        validate({})
    with pytest.raises(ConditionError):
        validate({"matches": "("})
    with pytest.raises(ConditionError):
        validate({"between": [1]})
    with pytest.raises(ConditionError):
        validate({"type": "thing"})
    validate({"eq": 1, "type": "int"})
