import logging
import time
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed, CoordinatorEntity

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
            driver = None
            try:
                driver = webdriver.Remote(command_executor=sel_url, options=options)
                # Prevents the script from hanging forever if the council site is slow
                driver.set_page_load_timeout(30)
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
                if driver: 
                    driver.quit()
                raise e

        try:
            html = await hass.async_add_executor_job(get_data)
            soup = BeautifulSoup(html, "html.parser")
            grids = soup.find_all("div", class_="waste-service-grid")
            
            # Record the timestamp of this successful update
            results = {
                "last_check": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
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
        except Exception as e:
            _LOGGER.error("Error updating Bromley Bins data: %s", e)
            raise UpdateFailed(f"Error communicating with Selenium: {e}")

    coordinator = DataUpdateCoordinator(
        hass, _LOGGER, name="Bromley Bins",
        update_method=async_update_data,
        update_interval=timedelta(hours=12),
    )

    # First refresh
    await coordinator.async_config_entry_first_refresh()
    
    # Add entities, filtering out the internal 'last_check' metadata key
    async_add_entities([
        BromleyBinSensor(coordinator, b) 
        for b in coordinator.data if b != "last_check"
    ])

class BromleyBinSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Bromley Bin Sensor linked to the Coordinator."""

    def __init__(self, coordinator, bin_type):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)
        self._bin_type = bin_type
        self._attr_name = f"Bromley {bin_type}"
        self._attr_unique_id = f"bromley_bins_{bin_type.lower().replace(' ', '_')}"
        self._attr_icon = "mdi:trash-can"

    @property
    def state(self):
        """Return the state from the coordinator data."""
        return self.coordinator.data.get(self._bin_type)

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return {
            "bin_type": self._bin_type,
            "last_check": self.coordinator.data.get("last_check"),
            "council": "Bromley"
        }
