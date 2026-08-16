from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
import json
import time
import logging
import re
import os

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class LinkedInParser:
    def __init__(self):
        # Initialize Chrome options
        self.options = webdriver.ChromeOptions()
        self.options.add_argument("--headless")  # Run in headless mode
        self.options.add_argument("--no-sandbox")
        self.options.add_argument("--disable-dev-shm-usage")
        self.options.add_argument("--disable-gpu")
        # Add user agent to avoid detection
        self.options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36"
        )

        # Initialize the driver
        self.driver = None
        self.profile_data = []

    def start_driver(self):
        """Start the Chrome driver"""
        if not self.driver:
            try:
                # Set up Chrome options
                self.options.add_argument("--headless")
                self.options.add_argument("--no-sandbox")
                self.options.add_argument("--disable-dev-shm-usage")
                self.options.add_argument("--disable-gpu")

                # Initialize ChromeDriver with proper architecture detection
                service = ChromeService()
                self.driver = webdriver.Chrome(service=service, options=self.options)
                self.driver.implicitly_wait(10)  # Wait up to 10 seconds for elements
            except Exception as e:
                logger.error(f"Error initializing ChromeDriver: {str(e)}")
                raise

    def close_driver(self):
        """Close the Chrome driver"""
        if self.driver:
            self.driver.quit()
            self.driver = None

    def navigate_to_page(self, url: str) -> bool:
        """Navigate to the specified URL"""
        try:
            if not self.driver:
                self.start_driver()
            self.driver.get(url)
            return True
        except Exception as e:
            logger.error(f"Error navigating to {url}: {str(e)}")
            return False

    def get_page_content(self) -> BeautifulSoup:
        """Get the current page content as BeautifulSoup object"""
        return BeautifulSoup(self.driver.page_source, "html.parser")

    def login(self, login_link: str):
        if not self.navigate_to_page(login_link):
            print("Error")
            return None

        time.sleep(5)  # Wait for content to load

        soup = self.get_page_content()
        body = soup.body
        login_section = body.find_all(string="Sign in")[0].parent.parent

        # Pull the specific input tag matches from BeautifulSoup
        soup_email = login_section.find_all("input")[0]
        soup_password = login_section.find_all("input")[1]
        soup_button = login_section.find_all(string="Sign in")[1].parent.parent.parent

        # Get global lists from the soup body to calculate exact DOM indices
        all_inputs = body.find_all("input")
        all_elements = body.find_all(True)  # Tracks every single element on the page

        # Calculate 1-based index offsets for XPath usage
        email_idx = all_inputs.index(soup_email) + 1
        password_idx = all_inputs.index(soup_password) + 1
        button_idx = all_elements.index(soup_button) + 1

        # Locate the specific nodes in Selenium using the exact global indices
        email_el = self.driver.find_element(By.XPATH, f"(//input)[{email_idx}]")
        password_el = self.driver.find_element(By.XPATH, f"(//input)[{password_idx}]")
        button_el = self.driver.find_element(By.XPATH, f"(//*)[{button_idx}]")

        # Interact using JavaScript execution to prevent "Not Interactable" failures
        # Force set the values into the fields
        self.driver.execute_script(
            "arguments[0].value = arguments[1];", email_el, "your_email@example.com"
        )
        self.driver.execute_script(
            "arguments[0].value = arguments[1];", password_el, "your_password"
        )

        # Force click the targeted sign in element container
        self.driver.execute_script("arguments[0].click();", button_el)

        print(login_section.find_all("input")[0].attrs)
        print(login_section.find_all("input")[1].attrs)

        return "Clicked Sign in"


def main():
    scraper = LinkedInParser()
    try:
        link = "https://www.linkedin.com/in/davidlee-peng/"
        results = scraper.login(
            "https://www.linkedin.com/login/?trk=guest_homepage-basic_nav-header-signin"
        )
        print(results)
    finally:
        scraper.close_driver()


if __name__ == "__main__":
    main()
