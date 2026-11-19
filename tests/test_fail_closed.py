import pytest

from app.core.credentials import extract_credentials, use_credentials
from app.core.frappe_client import (
    UserLoginRequired, enforce_user_login, get_frappe_client, get_service_client,
)


@pytest.fixture
def strict():
    enforce_user_login(True)
    yield
    enforce_user_login(False)          # never leak strict mode into other tests


def test_no_login_at_all_is_refused_when_strict(strict):
    with pytest.raises(UserLoginRequired):
        get_frappe_client()


def test_a_users_own_login_is_used(strict):
    with use_credentials(extract_credentials("token abc:def", None)):
        assert get_frappe_client().acts_as_user


def test_choosing_the_service_account_on_purpose_still_works(strict):
    with use_credentials(None):
        assert get_frappe_client() is get_service_client()


def test_the_choice_does_not_outlive_its_block(strict):
    with use_credentials(None):
        pass
    with pytest.raises(UserLoginRequired):
        get_frappe_client()


def test_a_user_block_inside_a_service_block_is_still_the_user(strict):
    with use_credentials(None):
        with use_credentials(extract_credentials("token a:b", None)):
            assert get_frappe_client().acts_as_user


def test_not_strict_keeps_the_old_behaviour_for_scripts():
    assert get_frappe_client() is get_service_client()