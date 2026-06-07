"""
Google Calendar integration for the Voice Agent's book_appointment tool.

Uses OAuth2 with a long-lived refresh token from environment variables:
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from core.config import settings

_TOKEN_URI = "https://oauth2.googleapis.com/token"
_SCOPES = ["https://www.googleapis.com/auth/calendar"]


@dataclass
class BookingResult:
    success: bool
    event_id: Optional[str] = None
    summary: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    html_link: Optional[str] = None
    error: Optional[str] = None


class GoogleCalendarTool:
    """Books appointments directly in Google Calendar via OAuth2 refresh-token flow."""

    def __init__(self) -> None:
        self._client_id = settings.google_client_id.get_secret_value()
        self._client_secret = settings.google_client_secret.get_secret_value()
        self._refresh_token = settings.google_refresh_token.get_secret_value()

    def _service(self):
        creds = Credentials(
            token=None,
            refresh_token=self._refresh_token,
            token_uri=_TOKEN_URI,
            client_id=self._client_id,
            client_secret=self._client_secret,
            scopes=_SCOPES,
        )
        creds.refresh(Request())
        return build("calendar", "v3", credentials=creds)

    def book(
        self,
        date: str,
        time: str,
        name: str,
        company: str,
        phone: str,
        topic: str,
        duration_minutes: int = 30,
        calendar_id: str = "primary",
        timezone: str = "Europe/Vienna",
    ) -> BookingResult:
        """
        Creates a Google Calendar event.

        Args:
            date: ISO date string (YYYY-MM-DD)
            time: 24h time string (HH:MM)
            name: Caller's full name
            company: Company name
            phone: Phone number
            topic: What they want to discuss
            duration_minutes: Appointment length (default 30)
            calendar_id: Target calendar (default 'primary')
            timezone: IANA timezone (default 'Europe/Vienna')

        Returns:
            BookingResult with event details or error information
        """
        try:
            tz = ZoneInfo(timezone)
            start_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            end_dt = start_dt + timedelta(minutes=duration_minutes)

            event_body = {
                "summary": f"Novara-Gespräch: {name} ({company})",
                "description": (
                    f"Gesprächsthema: {topic}\n"
                    f"Kontakt: {name}, {company}\n"
                    f"Telefon: {phone}"
                ),
                "start": {"dateTime": start_dt.isoformat(), "timeZone": timezone},
                "end": {"dateTime": end_dt.isoformat(), "timeZone": timezone},
            }

            created = (
                self._service()
                .events()
                .insert(calendarId=calendar_id, body=event_body)
                .execute()
            )

            return BookingResult(
                success=True,
                event_id=created["id"],
                summary=created["summary"],
                start=created["start"]["dateTime"],
                end=created["end"]["dateTime"],
                html_link=created.get("htmlLink"),
            )

        except Exception as exc:
            return BookingResult(success=False, error=str(exc))
