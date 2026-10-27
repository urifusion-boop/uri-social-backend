"""
A TikTok client is told nothing about other platforms in the objective list. Jane also echoes
the picked blurb into the chat ("Engagement it is — …"), so a Meta/WhatsApp comparison there
reaches the client twice. Meta's own list must stay exactly as it was.
"""
from app.agents.jane_ads.objectives import CHOICES, choices_for_platform


def test_tiktok_objective_wording_never_mentions_other_platforms():
    for o in choices_for_platform(tiktok=True):
        text = f'{o["label"]} {o["blurb"]} {o["caveat"]}'.lower()
        for word in ("meta", "instagram", "facebook", "whatsapp", "pixel"):
            assert word not in text, (o["value"], word)
        assert len(o["blurb"]) <= 70, o["blurb"]


def test_tiktok_keeps_the_same_choices_and_labels_as_meta():
    tiktok = choices_for_platform(tiktok=True)
    assert [(o["value"], o["label"]) for o in tiktok] == [(c["value"], c["label"]) for c in CHOICES]


def test_metas_list_is_unchanged():
    assert choices_for_platform(tiktok=False) == CHOICES
    assert any("WhatsApp orders" in c["blurb"] for c in CHOICES)
