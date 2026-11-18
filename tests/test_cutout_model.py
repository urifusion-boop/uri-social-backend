"""Background removal on our own server (app/utils/cutout_model.py)."""
import asyncio

import numpy as np
import pytest
from PIL import Image

from app.utils import background_removal, cutout_model


class _FakeInput:
    name = "input"


class _FakeSession:
    """Stands in for the ONNX model: 'foreground' is the middle third of the frame."""
    def __init__(self, value=None):
        self.value = value

    def get_inputs(self):
        return [_FakeInput()]

    def run(self, _outputs, feeds):
        size = feeds["input"].shape[-1]
        if self.value is not None:
            return [np.full((1, 1, size, size), self.value, dtype=np.float32)]
        pred = np.zeros((1, 1, size, size), dtype=np.float32)
        pred[..., size // 3: 2 * size // 3, size // 3: 2 * size // 3] = 1.0
        return [pred]


@pytest.fixture
def fake_model(monkeypatch):
    monkeypatch.setattr(cutout_model, "_session", _FakeSession())
    return cutout_model


def test_cut_out_keeps_the_photo_and_only_adds_transparency(fake_model):
    photo = Image.new("RGB", (300, 300), (200, 30, 90))
    out = fake_model.cut_out(photo)
    assert out.mode == "RGBA" and out.size == photo.size
    assert out.getpixel((150, 150)) == (200, 30, 90, 255)     # subject kept, pixels untouched
    assert out.getpixel((10, 10))[3] == 0                      # background clear


def test_no_clear_subject_is_not_a_cut_out(monkeypatch):
    monkeypatch.setattr(cutout_model, "_session", _FakeSession(value=0.0))
    assert cutout_model.cut_out(Image.new("RGB", (64, 64))) is None


def test_missing_model_means_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(cutout_model, "MODEL_DIR", str(tmp_path))
    assert not cutout_model.available()


def test_background_removal_says_no_when_the_model_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(cutout_model, "MODEL_DIR", str(tmp_path))
    assert asyncio.run(background_removal.remove_background_rembg("https://example.com/p.png")) is None
