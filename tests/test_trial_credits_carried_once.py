"""Subscribing during the free trial: the unused trial credits move to the wallet
once, the trial closes, and the balance is plan + trial (20 + 10 = 30), not 40."""
from datetime import datetime, timedelta

from app.services.CreditService import CreditService
from app.services.TrialService import TrialService

from tests.test_jane_ads_growth import mongomock_motor, run  # noqa: F401

U = "user-trial-1"


def services():
    db = mongomock_motor.AsyncMongoMockClient()["t"]
    trial, credit = TrialService(), CreditService()
    trial._db = db
    credit._db = db
    return db, trial, credit


def seed_trial(db, credits=10, days_left=7):
    now = datetime.utcnow()
    run(db.user_trials.insert_one({
        "user_id": U, "is_trial": True, "trial_used": True, "trial_credits": 10,
        "credits_remaining": credits, "trial_start_date": now - timedelta(days=7 - days_left),
        "trial_end_date": now + timedelta(days=days_left),
    }))


def subscribe(db, credit, carried, plan_credits=20):
    """What PaymentService._complete_payment does: plan credits, then the carried trial credits as bonus."""
    run(db.user_credits.insert_one({
        "user_id": U, "subscription_credits": plan_credits, "bonus_credits": 0, "total_credits": plan_credits,
        "credits_used": 0, "credits_remaining": plan_credits, "subscription_tier": "starter",
        "start_date": datetime.utcnow(),
        "created_at": datetime.utcnow(), "updated_at": datetime.utcnow(),
    }))
    if carried:
        run(credit.add_bonus_credits(user_id=U, bonus_amount=carried, reason="trial_credits_preserved"))


def balance(credit, trial, monkeypatch):
    import app.services.TrialService as T
    monkeypatch.setattr(T, "trial_service", trial)
    return run(credit.get_credit_balance(U))


def test_subscribing_moves_trial_credits_once_and_shows_30(monkeypatch):
    db, trial, credit = services()
    seed_trial(db)
    carried = run(trial.take_trial_credits_for_subscription(U))
    assert carried == 10
    subscribe(db, credit, carried)
    b = balance(credit, trial, monkeypatch)
    assert b.credits_remaining == 30
    st = run(trial.get_trial_status(U))
    assert st.trial_active is False and st.credits_remaining == 0
    assert st.trial_expired is False            # became a subscription; no "trial ended" messaging


def test_trial_credits_are_carried_only_once_even_if_confirmed_twice():
    db, trial, _ = services()
    seed_trial(db)
    assert run(trial.take_trial_credits_for_subscription(U)) == 10
    assert run(trial.take_trial_credits_for_subscription(U)) == 0


def test_existing_customer_who_already_shows_40_is_corrected_to_30(monkeypatch):
    # Subscribed under the old code: credits carried over, trial left open.
    db, trial, credit = services()
    seed_trial(db)
    subscribe(db, credit, carried=10)
    b = balance(credit, trial, monkeypatch)
    assert b.credits_remaining == 30
    doc = run(db.user_trials.find_one({"user_id": U}))
    assert doc["credits_remaining"] == 0 and doc["converted_to_subscription"] is True


def test_a_trial_user_without_a_subscription_keeps_their_trial():
    db, trial, _ = services()
    seed_trial(db, credits=7)
    st = run(trial.get_trial_status(U))
    assert st.trial_active is True and st.credits_remaining == 7


def test_new_plan_credits_work_the_same(monkeypatch):
    # The pricing update (Starter 45 credits): 45 + 10 carried = 55.
    db, trial, credit = services()
    seed_trial(db)
    subscribe(db, credit, run(trial.take_trial_credits_for_subscription(U)), plan_credits=45)
    assert balance(credit, trial, monkeypatch).credits_remaining == 55


def test_subscribed_before_the_trial_started_keeps_the_trial():
    # A wallet from before this trial (not "subscribed during the trial") is left alone.
    db, trial, credit = services()
    seed_trial(db, credits=5)
    run(db.user_credits.insert_one({"user_id": U, "subscription_tier": "starter", "subscription_credits": 20,
                                    "start_date": datetime.utcnow() - timedelta(days=60)}))
    st = run(trial.get_trial_status(U))
    assert st.trial_active is True and st.credits_remaining == 5
