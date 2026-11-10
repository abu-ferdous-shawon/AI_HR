import pytest

from app.core import frappe_client, media
from app.core.frappe_client import FrappeClient, FrappeError

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


class FakeResponse:
    def __init__(self, status=200, body=PNG):
        self.status_code = status
        self._body = body

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]

    def close(self):
        pass


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def client_with(response):
    client = FrappeClient("http://frappe.test:8001", "k", "s")
    client.session = FakeSession(response)
    return client


@pytest.fixture(autouse=True)
def clean():
    media._pictures.clear()
    yield
    media._pictures.clear()


def test_sniff_accepts_pictures_and_refuses_svg_and_text():
    assert media.sniff_image(PNG) == "image/png"
    assert media.sniff_image(b"\xff\xd8\xff" + b"0") == "image/jpeg"
    assert media.sniff_image(b"RIFF1234WEBPxxxx") == "image/webp"
    assert media.sniff_image(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>") is None
    assert media.sniff_image(b"hello") is None


def test_a_picture_belongs_to_its_owner():
    info = media.save_picture("u1", "p.png", PNG, "image/png")
    assert media.get_picture(info["id"], "u1").data == PNG
    assert media.get_picture(info["id"], "u2") is None


def test_old_pictures_expire(monkeypatch):
    info = media.save_picture("u1", "p.png", PNG, "image/png")
    monkeypatch.setattr(media, "TTL_SECONDS", -1)
    assert media.get_picture(info["id"], "u1") is None


def test_only_the_newest_pictures_are_kept(monkeypatch):
    monkeypatch.setattr(media, "MAX_ITEMS", 2)
    ids = [media.save_picture("u", f"{i}.png", PNG, "image/png")["id"] for i in range(3)]
    assert media.get_picture(ids[0], "u") is None
    assert media.get_picture(ids[2], "u") is not None


@pytest.mark.parametrize("url", ["/files/a.png", "/private/files/my photo.png",
                                 "/files/a.png?v=2", "/files/%C3%A9.png"])
def test_local_file_paths_are_accepted(url):
    assert FrappeClient._safe_file_path(url).startswith(("/files/", "/private/files/"))


@pytest.mark.parametrize("url", [
    "https://evil.example/a.png", "//evil.example/a.png", "javascript:alert(1)",
    "/etc/passwd", "/files/../private/x.png", "/files/%2e%2e/x.png",
    "files/a.png", "/files/a\\b.png", ""])
def test_everything_else_is_refused(url):
    with pytest.raises(FrappeError):
        FrappeClient._safe_file_path(url)


def test_get_file_returns_bytes_and_never_follows_redirects():
    client = client_with(FakeResponse())
    assert client.get_file("/files/a b.png") == (PNG, "image/png")
    url, kwargs = client.session.calls[0]
    assert url == "http://frappe.test:8001/files/a%20b.png"
    assert kwargs["allow_redirects"] is False


def test_get_file_refuses_redirects_svg_and_oversize(monkeypatch):
    with pytest.raises(FrappeError):
        client_with(FakeResponse(302)).get_file("/files/a.png")
    with pytest.raises(FrappeError):
        client_with(FakeResponse(body=b"<svg></svg>")).get_file("/files/a.svg")
    monkeypatch.setattr(frappe_client, "MAX_FILE_BYTES", 10)
    with pytest.raises(FrappeError):
        client_with(FakeResponse()).get_file("/files/a.png")


def test_an_external_address_never_reaches_the_network():
    client = client_with(FakeResponse())
    with pytest.raises(FrappeError):
        client.get_file("https://evil.example/a.png")
    assert client.session.calls == []