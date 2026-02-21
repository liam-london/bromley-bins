import logging
import time
import re
from datetime import datetime, timedelta, date

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .const import CONF_SELENIUM_URL, CONF_URL

_LOGGER = logging.getLogger(__name__)


def _parse_collection_date(text: str, now: date) -> date | None:
    """Parse the council 'Next collection' string into a date.

    Supports common WasteWorks formats like:
    - Today / Tomorrow
    - Friday 20 February
    - Friday, 20th February
    - Fri 20 Feb
    - Friday 20 February 2026
    - 20/02/2026
    - 2026-02-20
    """
    if not text:
        return None

    t = " ".join(text.strip().split())  # normalize whitespace
    lower = t.lower()

    if "today" in lower:
        return now
    if "tomorrow" in lower:
        return now + timedelta(days=1)

    # Remove common prefixes (some councils include extra words)
    t = re.sub(r"^(next\s+collection\s*:?)\s*", "", t, flags=re.I)

    # NEW: Normalize punctuation and ordinal suffixes
    # "Friday, 27th February" -> "Friday 27 February"
    t = t.replace(",", " ")
    t = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", t, flags=re.I)
    t = " ".join(t.split())

    # Try a set of known formats (includes abbreviated weekday/month)
    formats = [
        "%A %d %B %Y",
        "%A %d %B",
        "%a %d %b %Y",
        "%a %d %b",
        "%d %B %Y",
        "%d %B",
        "%d %b %Y",
        "%d %b",
        "%d/%m/%Y",
        "%d/%m/%y",
        "%Y-%m-%d",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(t, fmt)
            d = dt.date()
            # If the source omits the year, datetime defaults to 1900.
            if "%Y" not in fmt:
                d = d.replace(year=now.year)
                # If it looks like the date already passed (e.g., in Dec scraping Jan),
                # roll forward one year.
                if d < now - timedelta(days=7):
                    d = d.replace(year=now.year + 1)
            return d
        except ValueError:
            continue

    # Try to extract a date-like token from the string (last resort)
    m = re.search(r"(\d{4}-\d{2}-\d{2})", t)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            return None

    m = re.search(r"(\d{1,2}/\d{1,2}/\d{2,4})", t)
    if m:
        token = m.group(1)
        for fmt in ("%d/%m/%Y", "%d/%m/%y"):
            try:
                return datetime.strptime(token, fmt).date()
            except ValueError:
                pass

    return None


async def async_setup_entry(hass, config_entry, async_add_entities):
    url = config_entry.data[CONF_URL]
    sel_url = config_entry.data[CONF_SELENIUM_URL]

    async def async_update_data():
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
        )
        options.add_argument("--disable-blink-features=AutomationControlled")

        def get_data():
            driver = None
            try:
                driver = webdriver.Remote(command_executor=sel_url, options=options)
                # Prevent hanging forever if the council site is slow
                driver.set_page_load_timeout(30)
                driver.execute_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                driver.get(url)

                # WasteWorks wait logic
                found = False
                for _ in range(12):
                    if driver.find_elements(By.CLASS_NAME, "waste-service-grid"):
                        found = True
                        break
                    time.sleep(5)

                if not found:
                    raise Exception("WasteWorks grid not found")

                return driver.page_source
            finally:
                if driver:
                    driver.quit()

        try:
            html = await hass.async_add_executor_job(get_data)
            soup = BeautifulSoup(html, "html.parser")
            grids = soup.find_all("div", class_="waste-service-grid")

            now = dt_util.now().date()

            # Timestamp of this successful update (ISO, local time)
            results: dict[str, object] = {
                "last_check": dt_util.now().isoformat(timespec="seconds")
            }

            for grid in grids:
                name_tag = grid.find("h3", class_="waste-service-name")
                if not name_tag:
                    continue
                name = name_tag.get_text(strip=True)

                raw_date_val: str | None = None
                rows = grid.find_all("div", class_="govuk-summary-list__row")
                for row in rows:
                    key = row.find("dt")
                    if key and "Next collection" in key.get_text():
                        dd = row.find("dd")
                        raw_date_val = dd.get_text(strip=True) if dd else None
                        break

                if raw_date_val:
                    parsed = _parse_collection_date(raw_date_val, now)
                    # Store both parsed date and raw string for transparency/debugging
                    results[name] = parsed
                    results[f"{name}__raw"] = raw_date_val

            return results
        except Exception as e:
            _LOGGER.error("Error updating Bromley Bins data: %s", e)
            raise UpdateFailed(f"Error communicating with Selenium: {e}")

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="Bromley Bins",
        update_method=async_update_data,
        update_interval=timedelta(hours=12),
    )

    # First refresh
    await coordinator.async_config_entry_first_refresh()

    # Add entities, filtering out internal metadata keys
    async_add_entities(
        [
            BromleyBinSensor(coordinator, b)
            for b in coordinator.data
            if b != "last_check" and not str(b).endswith("__raw")
        ]
    )


class BromleyBinSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Bromley Bin Sensor linked to the Coordinator."""

    def __init__(self, coordinator: DataUpdateCoordinator, bin_type: str) -> None:
        super().__init__(coordinator)
        self._bin_type = bin_type
        self._attr_name = f"Bromley {bin_type}"
        self._attr_unique_id = f"bromley_bins_{bin_type.lower().replace(' ', '_')}"
        self._attr_icon = "mdi:trash-can"
        self._attr_device_class = SensorDeviceClass.DATE

    @property
    def native_value(self) -> date | None:
        """Return the parsed collection date (as a Python date)."""
        val = self.coordinator.data.get(self._bin_type)
        return val if isinstance(val, date) else None

    @property
    def extra_state_attributes(self):
        """Return additional attributes (including the raw council string)."""
        raw = self.coordinator.data.get(f"{self._bin_type}__raw")
        return {
            "bin_type": self._bin_type,
            "next_collection_raw": raw,
            "last_check": self.coordinator.data.get("last_check"),
            "council": "Bromley",
        }
