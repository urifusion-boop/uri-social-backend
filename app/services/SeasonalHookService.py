"""
Seasonal Hook Service - Nigerian Calendar-Aware Content Hooks
PRD Section 5: Where the Specific Hook Comes From

Generates genuinely specific, timely hooks based on Nigerian commercial calendar.
PRD Section 5.2: "The Nigerian Calendar Is a Gift Here"

Seasonal/calendar backbone + real trends on top = always contextual, never hollow.
"""
from datetime import datetime
from typing import Dict, Optional, Tuple


class SeasonalHookService:
    """
    PRD Section 5.1: Seasonal Backbone + Real Trends On Top

    Use seasonal/calendar layer as RELIABLE backbone - what's relevant for
    this kind of business at this exact time of year.
    """

    # Nigerian commercial calendar (PRD Section 5.2)
    # Format: month -> [(date_range, event_name, hook_template)]
    NIGERIAN_CALENDAR = {
        1: [  # January
            ((1, 7), "New Year", "New year, new goals — people are planning fresh starts"),
            ((8, 31), "January Hustle", "January is here and everyone's getting serious about their plans"),
        ],
        2: [  # February
            ((1, 10), "Pre-Valentine's", "Valentine's is coming up and people are already looking for gift ideas"),
            ((11, 14), "Valentine's Week", "Valentine's is here and everyone's hunting for that perfect gift/experience"),
            ((15, 28), "Post-Valentine", "February energy — couples are still celebrating, singles are treating themselves"),
        ],
        3: [  # March
            ((1, 31), "Easter Prep", "Easter season is approaching and people start planning celebrations"),
        ],
        4: [  # April
            ((1, 15), "Easter Week", "Easter celebrations are happening — families gathering, events everywhere"),
            ((16, 30), "Post-Easter", "People are back from Easter break and settling into routines"),
        ],
        5: [  # May
            ((1, 31), "Mid-Year Planning", "We're approaching mid-year — businesses are reviewing goals"),
        ],
        6: [  # June
            ((1, 30), "Mid-Year Push", "Mid-year — time to double down on what's working"),
        ],
        7: [  # July
            ((1, 31), "Summer Season", "School holidays mean families have more time for activities"),
        ],
        8: [  # August
            ((1, 31), "Back to School Prep", "Back-to-school season is starting and parents are shopping"),
        ],
        9: [  # September
            ((1, 30), "Back to School", "School is back in session — new routines, new opportunities"),
            ((15, 30), "Independence Prep", "Independence Day is coming — patriotic vibes everywhere"),
        ],
        10: [  # October
            ((1, 7), "Independence Week", "Nigeria's Independence Day — national pride is high"),
            ((8, 31), "Detty December Prep", "People are already planning for Detty December"),
        ],
        11: [  # November
            ((1, 23), "Black Friday Prep", "Black Friday is approaching — everyone's watching for deals"),
            ((24, 30), "Black Friday Week", "Black Friday is here and shoppers are hunting for the best deals"),
        ],
        12: [  # December
            ((1, 20), "Detty December", "Detty December is here — parties, Owambe, celebrations everywhere"),
            ((21, 25), "Christmas Week", "Christmas week — festive mood at its peak"),
            ((26, 31), "Year End", "Year wrapping up — reflections, celebrations, new year prep"),
        ],
    }

    # Industry-specific seasonal angles
    INDUSTRY_ANGLES = {
        "fashion": {
            "keywords": ["ankara", "clothing", "apparel", "style", "outfit"],
            "seasonal_hooks": {
                "Owambe": "Owambe season is picking up and everybody's hunting for standout styles",
                "December": "Detty December means everyone needs fresh outfits for all the parties",
                "Valentine's": "Valentine's is coming and couples want matching or special outfits",
                "Independence": "Independence Day celebrations — people love patriotic colors",
            }
        },
        "food": {
            "keywords": ["restaurant", "food", "jollof", "catering", "bakery", "cake"],
            "seasonal_hooks": {
                "Friday": "Fridays are when Lagos starts planning weekend food",
                "Owambe": "Owambe season means catering orders are flying",
                "Christmas": "Christmas orders are piling up — everyone needs that perfect menu",
                "Valentine's": "Valentine's bookings always spike around now",
            }
        },
        "skincare": {
            "keywords": ["skincare", "beauty", "cosmetics", "skin", "glow"],
            "seasonal_hooks": {
                "Harmattan": "Harmattan's here and everyone's dealing with dry, ashy skin",
                "December": "Detty December means everyone wants to glow for the parties",
                "Valentine's": "Valentine's prep — people want their skin looking perfect",
            }
        },
        "interior": {
            "keywords": ["interior", "decor", "furniture", "home"],
            "seasonal_hooks": {
                "December": "This is when people start planning to redo their space before festive season",
                "New Year": "New year, new home vibes — people are redecorating",
            }
        },
        "events": {
            "keywords": ["event", "planning", "venue", "photography"],
            "seasonal_hooks": {
                "Owambe": "Owambe season is here — events every weekend",
                "December": "December events are booking up fast",
                "Wedding Season": "Wedding season is here and couples are finalizing vendors",
            }
        },
        "fitness": {
            "keywords": ["fitness", "gym", "workout", "health"],
            "seasonal_hooks": {
                "New Year": "New year resolutions mean everyone's thinking about fitness goals",
                "December": "Pre-December fitness — people want to look good for the parties",
            }
        },
    }

    @staticmethod
    def get_current_seasonal_event() -> Tuple[str, str]:
        """
        Get current seasonal event based on today's date.
        Returns: (event_name, hook_description)
        """
        now = datetime.now()
        month = now.month
        day = now.day

        if month in SeasonalHookService.NIGERIAN_CALENDAR:
            events = SeasonalHookService.NIGERIAN_CALENDAR[month]
            for date_range, event_name, hook in events:
                start_day, end_day = date_range
                if start_day <= day <= end_day:
                    return (event_name, hook)

        # Fallback to generic month hook
        return ("General", "This is a good time to connect with your audience")

    @staticmethod
    def match_industry_to_season(industry: str, business_name: str) -> Dict[str, str]:
        """
        PRD Section 5: Generate genuinely specific hook for industry + season.

        Returns dict with:
        - proof_listening: References business (PRD 4.1)
        - timely_hook: Specific seasonal angle (PRD 4.2)
        - offer: Low-effort offer (PRD 4.3)
        - seed_content: What to generate
        """
        industry_lower = industry.lower() if industry else ""
        event_name, base_hook = SeasonalHookService.get_current_seasonal_event()

        # Try to match industry to specific seasonal angle
        matched_hook = None
        matched_industry_type = None

        for industry_type, config in SeasonalHookService.INDUSTRY_ANGLES.items():
            keywords = config["keywords"]
            if any(keyword in industry_lower for keyword in keywords):
                matched_industry_type = industry_type
                # Check if we have a hook for current season
                seasonal_hooks = config.get("seasonal_hooks", {})
                for season_key, hook in seasonal_hooks.items():
                    if season_key.lower() in event_name.lower() or season_key.lower() in base_hook.lower():
                        matched_hook = hook
                        break
                if matched_hook:
                    break

        # Build message components (PRD Section 4)
        if matched_hook:
            timely_hook = matched_hook
        else:
            timely_hook = base_hook

        # PRD 4.1: Prove it was listening
        proof_listening = f"Saw you're doing {business_name or industry} in Lagos"

        # PRD 4.3: Low-effort offer
        offer = f"want me to make you a post about it?"

        # Seed content for generation
        if matched_industry_type == "food" and "Friday" in timely_hook:
            seed_content = "Friday special post highlighting your signature dish"
        elif matched_industry_type == "fashion" and "Owambe" in timely_hook:
            seed_content = "Post showcasing your Ankara designs for Owambe season"
        elif matched_industry_type == "skincare" and "Harmattan" in timely_hook:
            seed_content = "Post about beating harmattan dryness with your products"
        else:
            seed_content = f"Post relevant to {event_name} for {industry}"

        return {
            "proof_listening": proof_listening,
            "timely_hook": timely_hook,
            "offer": offer,
            "seed_content": seed_content,
            "event_name": event_name,
        }

    @staticmethod
    def generate_first_message(brand_name: str, industry: str, location: str = "Lagos") -> str:
        """
        PRD Section 4: Generate Jane's first message with all 4 parts.

        1. Prove it was listening
        2. Genuinely specific, timely hook
        3. Low-effort offer
        4. One clear next step

        PRD Section 6: Tone like a knowledgeable friend, not a growth prompt.
        """
        hook_data = SeasonalHookService.match_industry_to_season(industry, brand_name)

        # Build message (PRD Section 7: Worked Examples)
        if brand_name:
            greeting = f"Hey! Saw you're doing {brand_name} in {location}."
        else:
            greeting = f"Hey! Saw you're running a {industry} business in {location}."

        timely_hook = hook_data["timely_hook"]
        offer = hook_data["offer"]

        # PRD Section 6: Short, warm, casual
        message = f"{greeting} {timely_hook} — {offer}"

        return message
