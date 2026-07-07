# app/agents/social_media_manager/services/holiday_calendar_service.py

from datetime import datetime
from typing import Any, Dict, List, Optional


class HolidayCalendarService:
    """
    Detect holidays and seasonal events based on region and industry.
    Returns upcoming holidays within the planning window.

    PRD Requirement: Section 8 - Holidays & Seasonal Events
    "The engine should automatically identify important dates and recommend
    content before the event where appropriate."
    """

    # Global holidays (most regions)
    GLOBAL_HOLIDAYS = {
        "01-01": {"name": "New Year's Day", "type": "cultural", "lead_time_weeks": 2},
        "02-14": {"name": "Valentine's Day", "type": "commercial", "lead_time_weeks": 3},
        "03-08": {"name": "International Women's Day", "type": "cultural", "lead_time_weeks": 2},
        "04-22": {"name": "Earth Day", "type": "cultural", "lead_time_weeks": 1},
        "05-01": {"name": "Labour Day", "type": "cultural", "lead_time_weeks": 1},
        "10-10": {"name": "World Mental Health Day", "type": "cultural", "lead_time_weeks": 1},
        "11-25": {"name": "Black Friday", "type": "commercial", "lead_time_weeks": 4},  # Approximate
        "12-25": {"name": "Christmas", "type": "cultural", "lead_time_weeks": 6},
        "12-31": {"name": "New Year's Eve", "type": "cultural", "lead_time_weeks": 2},
    }

    # Nigeria-specific holidays
    NIGERIA_HOLIDAYS = {
        "10-01": {"name": "Independence Day", "type": "national", "lead_time_weeks": 2},
        "05-29": {"name": "Democracy Day", "type": "national", "lead_time_weeks": 1},
        "10-07": {"name": "Lagos State Day", "type": "regional", "lead_time_weeks": 1},
    }

    # Industry-specific awareness days
    INDUSTRY_DAYS = {
        "technology": {
            "05-17": {"name": "World Telecom Day", "type": "industry", "lead_time_weeks": 1},
            "10-24": {"name": "World Development Information Day", "type": "industry", "lead_time_weeks": 1},
        },
        "health": {
            "04-07": {"name": "World Health Day", "type": "industry", "lead_time_weeks": 2},
            "10-10": {"name": "World Mental Health Day", "type": "industry", "lead_time_weeks": 2},
        },
        "fashion": {
            "04-24": {"name": "Fashion Revolution Day", "type": "industry", "lead_time_weeks": 1},
        },
        "food": {
            "10-16": {"name": "World Food Day", "type": "industry", "lead_time_weeks": 1},
        },
        "finance": {
            "03-15": {"name": "World Consumer Rights Day", "type": "industry", "lead_time_weeks": 1},
        },
        "real estate": {
            "03-31": {"name": "World Backup Day", "type": "industry", "lead_time_weeks": 1},  # Home safety angle
        },
        "e-commerce": {
            "11-25": {"name": "Black Friday", "type": "industry", "lead_time_weeks": 4},
            "11-28": {"name": "Cyber Monday", "type": "industry", "lead_time_weeks": 4},
        },
    }

    # Relevance scores by industry and holiday type
    RELEVANCE_MATRIX = {
        "e-commerce": {"commercial": 1.0, "cultural": 0.8, "industry": 0.9, "national": 0.5},
        "fashion": {"commercial": 1.0, "cultural": 0.8, "industry": 0.9, "national": 0.5},
        "food": {"commercial": 0.9, "cultural": 0.9, "industry": 1.0, "national": 0.6},
        "technology": {"commercial": 0.6, "cultural": 0.7, "industry": 1.0, "national": 0.5},
        "finance": {"commercial": 0.5, "cultural": 0.7, "industry": 0.9, "national": 0.6},
        "health": {"commercial": 0.6, "cultural": 0.9, "industry": 1.0, "national": 0.5},
        "real estate": {"commercial": 0.7, "cultural": 0.8, "industry": 0.9, "national": 0.6},
        "default": {"commercial": 0.7, "cultural": 0.8, "industry": 0.8, "national": 0.6},
    }

    @staticmethod
    def get_upcoming_holidays(
        week_start: str,
        region: str = "",
        industry: str = ""
    ) -> List[Dict[str, Any]]:
        """
        Returns holidays occurring in or near the planning week.

        Args:
            week_start: ISO date string "YYYY-MM-DD"
            region: User's region (e.g., "Nigeria", "West Africa")
            industry: User's industry category

        Returns:
            List of holiday dicts with date, name, relevance, and content angles.

        Example:
            [
                {
                    "date": "2025-02-14",
                    "name": "Valentine's Day",
                    "type": "commercial",
                    "lead_time_days": 25,
                    "relevance_score": 0.9,
                    "content_angle": "Start promoting gift ideas and romantic products 3 weeks before Valentine's.",
                }
            ]
        """
        try:
            week_start_dt = datetime.strptime(week_start, "%Y-%m-%d")
        except ValueError:
            print(f"⚠️ Invalid week_start format: {week_start}")
            return []

        year = week_start_dt.year

        # Combine all relevant holiday sources
        all_holidays = dict(HolidayCalendarService.GLOBAL_HOLIDAYS)

        # Add Nigeria-specific if region matches
        if region and "nigeria" in region.lower():
            all_holidays.update(HolidayCalendarService.NIGERIA_HOLIDAYS)

        # Add industry-specific days
        industry_lower = industry.lower().replace(" ", "_") if industry else ""
        if industry_lower in HolidayCalendarService.INDUSTRY_DAYS:
            all_holidays.update(HolidayCalendarService.INDUSTRY_DAYS[industry_lower])

        # Get relevance matrix for this industry
        relevance_map = HolidayCalendarService.RELEVANCE_MATRIX.get(
            industry_lower,
            HolidayCalendarService.RELEVANCE_MATRIX["default"]
        )

        upcoming = []

        for date_str, holiday_data in all_holidays.items():
            # Parse MM-DD and construct full date
            try:
                month, day = map(int, date_str.split("-"))
                holiday_dt = datetime(year, month, day)
            except ValueError:
                continue

            # Calculate lead time
            delta = (holiday_dt - week_start_dt).days
            lead_time_weeks = holiday_data.get("lead_time_weeks", 1)
            recommended_lead_days = lead_time_weeks * 7

            # Include holidays within their lead time window OR during the week
            if -7 <= delta <= recommended_lead_days:
                holiday_type = holiday_data.get("type", "cultural")
                relevance = relevance_map.get(holiday_type, 0.7)

                # Generate content angle based on lead time and type
                content_angle = HolidayCalendarService._generate_content_angle(
                    holiday_data["name"],
                    holiday_type,
                    delta,
                    industry
                )

                upcoming.append({
                    "date": holiday_dt.strftime("%Y-%m-%d"),
                    "name": holiday_data["name"],
                    "type": holiday_type,
                    "lead_time_days": delta,
                    "relevance_score": relevance,
                    "content_angle": content_angle,
                })

        # Sort by relevance and proximity
        upcoming.sort(key=lambda h: (-h["relevance_score"], h["lead_time_days"]))

        return upcoming[:5]  # Return top 5 most relevant

    @staticmethod
    def _generate_content_angle(
        holiday_name: str,
        holiday_type: str,
        days_until: int,
        industry: str
    ) -> str:
        """
        Generate content recommendation angle based on holiday and timing.
        """
        if days_until <= 0:
            # Holiday is today or passed
            return f"Create timely content celebrating {holiday_name} today."

        if days_until <= 7:
            # Within the week
            return f"{holiday_name} is this week. Create last-minute content or reminders."

        if days_until <= 14:
            # 1-2 weeks out
            if holiday_type == "commercial":
                return f"{holiday_name} is in 2 weeks. Ramp up promotional content and special offers."
            return f"{holiday_name} is approaching. Start building awareness and anticipation."

        # More than 2 weeks out
        if holiday_type == "commercial":
            if "valentine" in holiday_name.lower():
                return "Start promoting gift ideas and romantic products 3 weeks before Valentine's Day for maximum sales."
            if "christmas" in holiday_name.lower():
                return "Begin holiday season content early. Introduce gift guides, special collections, and holiday promotions."
            if "black friday" in holiday_name.lower():
                return "Build anticipation for Black Friday deals. Tease upcoming offers and create urgency."
            return f"Start early promotions for {holiday_name} to capture planning shoppers."

        return f"Begin creating awareness content for {holiday_name} to position your brand early."
