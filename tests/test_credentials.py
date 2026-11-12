import pytest

from app.core.credentials import (
    current_holder, extract_credentials, use_credentials,
)
from app.core.frappe_client import (
    FrappeClient, FrappeError, get_frappe_client, get_service_client,
)

SID = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


@pytest.mark.parametrize("auth, cookie, expected", [
    ("token abc:def", None, {"Authorization": "token abc:def"}),
    ("TOKEN abc:def", None, {"Authorization": "TOKEN abc:def"}),
    ("Bearer abcdef123", None, {"Authorization": "Bearer abcdef123"}),
    (None, f"sid={SID}", {"Cookie": f"sid={SID}"}),
    (None, f"theme=dark; sid={SID}; user_id=a%40b.com", {"Cookie": f"sid={SID}"}),
    ("token abc:def", f"sid={SID}", {"Authorization": "token abc:def"}),   # the header wins
])
def test_credentials_are_extracted_and_minimised(auth, cookie, expected):
    assert extract_credentials(auth, cookie).headers() == expected


@pytest.mark.parametrize("auth, cookie", [
    (None, None), ("", ""), ("Basic dXNlcjpwYXNz", None), ("token nocolon", None),
    ("token a:b\r\nX-Evil: 1", None), (None, "sid=Guest"), (None, "other=1"),
    (None, "sid=short"), (None, "sid=abc def ghi1234"), (None, "sid=" + "x" * 300),
])
def test_anything_else_gives_no_credentials(auth, cookie):
    assert extract_credentials(auth, cookie) is None


def test_credentials_never_show_up_in_text():
    for creds in (extract_credentials("token abc:def", None),
                  extract_credentials(None, f"sid={SID}")):
        shown = repr(creds) + str(creds) + f"{creds}"
        assert "abc" not in shown and SID not in shown


def test_clients_act_as_the_user_inside_the_block_and_as_the_key_outside():
    service = get_service_client()
    assert current_holder() is None and get_frappe_client() is service
    creds = extract_credentials("token abc:def", None)
    with use_credentials(creds):
        user = get_frappe_client()
        assert user is not service and user.acts_as_user
        assert user.session.headers["Authorization"] == "token abc:def"
        assert get_frappe_client() is user                      # one client per request
    assert current_holder() is None and get_frappe_client() is service
    assert service.session.headers["Authorization"] != "token abc:def"
    assert get_service_client() is service                      # built once


def test_no_credentials_means_the_service_account_explicitly():
    with use_credentials(None):
        assert get_frappe_client() is get_service_client()


def test_nested_blocks_restore_the_outer_user():
    first = extract_credentials("token a1:b1", None)
    second = extract_credentials("token a2:b2", None)
    with use_credentials(first):
        with use_credentials(second):
            assert get_frappe_client().session.headers["Authorization"] == "token a2:b2"
        assert get_frappe_client().session.headers["Authorization"] == "token a1:b1"


def test_the_user_client_is_closed_when_the_block_ends():
    closed = []
    with use_credentials(extract_credentials("token abc:def", None)):
        get_frappe_client().session.close = lambda: closed.append(True)
    assert closed == [True]


def test_a_cookie_login_sends_only_the_session_id():
    client = FrappeClient("http://x", credentials=extract_credentials(
        None, f"theme=dark; sid={SID}"))
    assert client.session.headers["Cookie"] == f"sid={SID}"
    assert "Authorization" not in client.session.headers


class Replies:
    def __init__(self, status, body=None):
        self.status, self.body, self.kwargs = status, body or {"data": []}, None

    def get(self, url, **kwargs):
        self.kwargs = kwargs
        status, body = self.status, self.body
        return type("Reply", (), {"status_code": status, "reason": "x", "text": "",
                                  "json": lambda self: body})()


def test_redirects_are_never_followed_and_mean_an_expired_login():
    client = FrappeClient("http://x", "k", "s")
    client.session = Replies(302)
    with pytest.raises(FrappeError) as err:
        client.get_doc("Employee", "E1")
    assert err.value.status_code == 401 and client.session.kwargs["allow_redirects"] is False
    client.session = Replies(200, {"data": [{"name": "E1"}]})
    assert client.get_list("Employee") == [{"name": "E1"}]
    assert client.session.kwargs["allow_redirects"] is False