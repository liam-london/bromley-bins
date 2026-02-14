import logging
import time
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, CONF_URL, CONF_SELENIUM_URL

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, config_entry, async_add_entities):
    url = config_entry.data[CONF_URL]
    sel_url = config_entry.data[CONF_SELENIUM_URL]

    async def async_update_data():
        options = Options()
        options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36')
        options.add_argument('--disable-blink-features=AutomationControlled')
        
        def get_data():
            try:
                driver = webdriver.Remote(command_executor=sel_url, options=options)
                driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                driver.get(url)
                
                # WasteWorks Wait Logic
                found = False
                for _ in range(12):
                    if driver.find_elements(By.CLASS_NAME, "waste-service-grid"):
                        found = True
                        break
                    time.sleep(5)
                
                if not found:
                    driver.quit()
                    raise Exception("WasteWorks grid not found")
                
                html = driver.page_source
                driver.quit()
                return html
            except Exception as e:
                if 'driver' in locals(): driver.quit()
                raise e

        html = await hass.async_add_executor_job(get_data)
        soup = BeautifulSoup(html, "html.parser")
        grids = soup.find_all("div", class_="waste-service-grid")
        
        results = {}
        for grid in grids:
            name_tag = grid.find("h3", class_="waste-service-name")
            if not name_tag: continue
            name = name_tag.get_text(strip=True)
            
            date_val = None
            rows = grid.find_all("div", class_="govuk-summary-list__row")
            for row in rows:
                key = row.find("dt")
                if key and "Next collection" in key.get_text():
                    date_val = row.find("dd").get_text(strip=True)
                    break
            if date_val:
                results[name] = date_val
        return results

    coordinator = DataUpdateCoordinator(
        hass, _LOGGER, name="Bromley Bins",
        update_method=async_update_data,
        update_interval=timedelta(hours=12),
    )

    await coordinator.async_config_entry_first_refresh()
    async_add_entities([BromleyBinSensor(coordinator, b) for b in coordinator.data])

class BromleyBinSensor(SensorEntity):
    def __init__(self, coordinator, bin_type):
        self.coordinator = coordinator
        self._bin_type = bin_type
        # Setting attributes to match the original plugin style
        self._attr_name = f"Bromley {bin_type}"
        self._attr_unique_id = f"bromley_bins_{bin_type.lower().replace(' ', '_')}"
        self._attr_icon = "mdi:trash-can"

    @property
    def state(self):
        """Return the date string."""
        return self.coordinator.data.get(self._bin_type)

    @property
    def extra_state_attributes(self):
        """Expose attributes like the old plugin did."""
        return {
            "bin_type": self._bin_type,
            "last_check": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "council": "Bromley"
        }
