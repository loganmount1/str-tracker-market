"""Base collector class and shared data structures."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class CalendarDay:
    date: str  # YYYY-MM-DD
    available: bool
    price: Optional[float] = None
    price_currency: str = "USD"
    min_nights: Optional[int] = None


@dataclass
class PropertyDetails:
    name: Optional[str] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    max_guests: Optional[int] = None
    property_type: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    amenities: list = field(default_factory=list)
    overall_rating: Optional[float] = None
    review_count: Optional[int] = None
    superhost: Optional[bool] = None
    host_name: Optional[str] = None
    thumbnail_url: Optional[str] = None


@dataclass
class ReviewData:
    overall_rating: Optional[float] = None
    review_count: Optional[int] = None
    cleanliness_rating: Optional[float] = None
    accuracy_rating: Optional[float] = None
    checkin_rating: Optional[float] = None
    communication_rating: Optional[float] = None
    location_rating: Optional[float] = None
    value_rating: Optional[float] = None


class BaseCollector(ABC):
    """Abstract base for all platform collectors."""

    platform: str = ""

    @abstractmethod
    def extract_id(self, url: str) -> str:
        """Extract platform-specific property ID from URL."""
        pass

    @abstractmethod
    def collect_calendar(self, property_id: str) -> list[CalendarDay]:
        """Collect calendar availability and pricing."""
        pass

    @abstractmethod
    def collect_details(self, property_id: str) -> PropertyDetails:
        """Collect property static details."""
        pass

    @abstractmethod
    def collect_reviews(self, property_id: str) -> ReviewData:
        """Collect review summary data."""
        pass
