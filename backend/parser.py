from __future__ import annotations

import argparse
import getpass
import json
import logging
import re
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup, Tag
from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent
SESSION_PATH = BACKEND_DIR / ".linkedin_session.json"
ENV_LOCAL_PATH = BACKEND_DIR / ".env.local"
PROFILES_DIR = PROJECT_DIR / "profiles"

load_dotenv(BACKEND_DIR / ".env")
load_dotenv(PROJECT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class LinkedInAuthError(RuntimeError):
    """Raised when LinkedIn rejects a login or the login page cannot be used."""


def cli_command() -> str:
    return f"python {sys.argv[0]}"


def env_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def env_unquote(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        inner = text[1:-1]
        if text[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return text


def load_env_local() -> dict[str, str]:
    if not ENV_LOCAL_PATH.exists():
        return {}
    values: dict[str, str] = {}
    for line in ENV_LOCAL_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = env_unquote(value)
    return values


def save_env_local(email: str, password: str) -> None:
    values = load_env_local()
    values["LINKEDIN_EMAIL"] = email
    values["LINKEDIN_PASSWORD"] = password
    values.pop("LINKEDIN_PASSWORD_HASH", None)
    body = "".join(f"{key}={env_quote(value)}\n" for key, value in values.items())
    ENV_LOCAL_PATH.write_text(body, encoding="utf-8")


def saved_credentials() -> tuple[str, str] | None:
    stored = load_env_local()
    email = stored.get("LINKEDIN_EMAIL", "").strip()
    password = stored.get("LINKEDIN_PASSWORD", "")
    if email and password:
        return email, password
    return None


def prompt_credentials() -> tuple[str, str]:
    stored = load_env_local()
    saved_email = stored.get("LINKEDIN_EMAIL", "").strip()

    if saved_email:
        entered = input(f"Email [{saved_email}]: ").strip()
        email = entered or saved_email
    else:
        email = input("Email: ").strip()
    while not email:
        email = input("Email: ").strip()

    while True:
        password = getpass.getpass("Password: ")
        if password:
            return email, password
        print("Password cannot be empty.")


def print_available_commands() -> None:
    cmd = cli_command()
    print()
    print("Available commands:")
    print(f"  {cmd} --login")
    print("      (save or update your LinkedIn email and password)")
    print(f"  {cmd} <profile_url>")
    print("      (scrape a LinkedIn profile and save JSON under profiles/)")
    print(f"  {cmd} <profile_url> -o profile.json")
    print("      (also copy the JSON to a custom file path)")
    print(f"  {cmd} --html test.html")
    print("      (parse a saved HTML file without logging in)")
    print(f"  {cmd} <profile_url> --no-details")
    print("      (scrape only the main profile page, skip extra detail pages)")
    print(f"  {cmd} <profile_url> --headless")
    print("      (run Chrome in the background without a window)")


MONTHS = (
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
)
DATE_RANGE_RE = re.compile(
    rf"^(?:{MONTHS}\s+)?(?:\d{{4}}|\d{{1,2}}/\d{{4}})\s*[-–]\s*"
    rf"(?:Present|(?:{MONTHS}\s+)?(?:\d{{4}}|\d{{1,2}}/\d{{4}}))"
    rf"(?:\s*·\s*(.+))?$",
    re.I,
)
DURATION_RE = re.compile(
    r"^(?:\d+\s*(?:yr|yrs|year|years)(?:\s+\d+\s*(?:mo|mos|month|months))?|"
    r"\d+\s*(?:mo|mos|month|months))$",
    re.I,
)
EMPLOYMENT_RE = re.compile(
    r"(full-time|part-time|self-employed|freelance|contract|"
    r"internship|apprenticeship|seasonal|permanent)",
    re.I,
)
WORK_MODE_RE = re.compile(r"^(on-site|remote|hybrid)(?:\s*[·,].*)?$", re.I)
SKILL_COUNT_RE = re.compile(r"(?:and\s+)?\+\d+\s+skills$", re.I)
FOLLOWERS_RE = re.compile(r"([\d,.]+)\s+followers?", re.I)
DEGREE_RE = re.compile(
    r"\b(BESc|BASc|BSc|BEng|BFA|BA|BS|MS|MSc|MEng|MBA|PhD|MA|"
    r"Bachelor|Master|Doctor|Diploma|Certificate|Associate)\b",
    re.I,
)
GEO_RE = re.compile(
    r"\b(Canada|United States|USA|UK|United Kingdom|Australia|Germany|"
    r"India|Area|Region|Ontario|Quebec|Alberta|Remote|On-site|Hybrid|"
    r"Metropolitan)\b",
    re.I,
)
ORG_RE = re.compile(
    r"\b(University|College|Inc\.?|Corp\.?|LLC|Ltd\.?|Limited|Group|"
    r"Company|Institute|Solutions|Engineers)\b",
    re.I,
)
NOISE_TEXT = {
    "contact info",
    "message",
    "follow",
    "connect",
    "more",
    "see more",
    "show less",
    "show credential",
    "1st",
    "2nd",
    "3rd",
    "· 1st",
    "· 2nd",
    "· 3rd",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
LOGIN_EMAIL_SELECTORS = [
    (By.ID, "username"),
    (By.NAME, "session_key"),
    (By.CSS_SELECTOR, "input[autocomplete='username']"),
    (By.CSS_SELECTOR, "input[type='email']"),
]
LOGIN_PASSWORD_SELECTORS = [
    (By.ID, "password"),
    (By.NAME, "session_password"),
    (By.CSS_SELECTOR, "input[type='password']"),
]
LOGIN_ERROR_SELECTORS = [
    "#error-for-username",
    "#error-for-password",
    ".form__label--error",
    ".alert-content",
    "[role='alert']",
    ".login__form .error",
]


def clean_text(value: str) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    text = re.sub(r"[….]?\s*(see )?more$", "", text, flags=re.I).strip()
    text = re.sub(r"\s*show less$", "", text, flags=re.I).strip()
    return text


def tag_text(el: Tag | None) -> str:
    if not el:
        return ""
    return clean_text(el.get_text(" ", strip=True))


def is_noise(text: str) -> bool:
    lowered = text.lower().strip(" ·")
    if not lowered or lowered in NOISE_TEXT:
        return True
    if lowered.startswith("show all"):
        return True
    if SKILL_COUNT_RE.search(text):
        return True
    return False


def paragraphs(el: Tag | None) -> list[str]:
    if not el:
        return []
    texts: list[str] = []
    for p in el.find_all("p"):
        hrefs = " ".join(anchor.get("href") or "" for anchor in p.find_all("a"))
        if "skill-associations-details" in hrefs:
            continue
        text = tag_text(p)
        if text and not is_noise(text) and text not in texts:
            texts.append(text)
    return texts


def split_date_and_duration(text: str) -> tuple[str | None, str | None]:
    match = DATE_RANGE_RE.match(text)
    if not match:
        return None, None
    extra = (match.group(1) or "").strip()
    dates = text.split("·", 1)[0].strip()
    duration = extra if extra and DURATION_RE.match(extra) else None
    return dates, duration


def split_company_line(text: str) -> tuple[str, str | None]:
    if " · " not in text:
        return text, None
    left, right = text.split(" · ", 1)
    if EMPLOYMENT_RE.search(right):
        return left.strip(), right.strip()
    return text, None


def looks_like_location(text: str) -> bool:
    if WORK_MODE_RE.match(text):
        return True
    if DATE_RANGE_RE.match(text) or DURATION_RE.match(text) or DEGREE_RE.search(text):
        return False
    if ORG_RE.search(text):
        return False
    return "," in text and bool(GEO_RE.search(text)) and len(text) < 90


def collection_items(section: Tag | None) -> list[Tag]:
    if not section:
        return []
    items: list[Tag] = []
    for tag in section.find_all(True):
        key = str(tag.get("componentkey") or "")
        if not key.startswith("entity-collection-item"):
            continue
        nested_in_item = tag.find_parent(
            attrs={
                "componentkey": lambda value: bool(value)
                and str(value).startswith("entity-collection-item")
            }
        )
        if nested_in_item:
            continue
        items.append(tag)
    return items


def find_by_testid_prefix(soup: Tag, prefix: str) -> Tag | None:
    return soup.find(
        attrs={"data-testid": lambda value: bool(value) and str(value).startswith(prefix)}
    )


def find_by_component_suffix(soup: Tag, suffix: str) -> Tag | None:
    return soup.find(
        attrs={
            "componentkey": lambda value: bool(value) and str(value).endswith(suffix)
        }
    )


def section_by_heading(soup: Tag, heading: str) -> Tag | None:
    target = heading.lower()
    for h2 in soup.find_all("h2"):
        text = tag_text(h2).lower()
        if text == target or text.startswith(f"{target} (") or text.startswith(f"{target} &"):
            return h2.find_parent("section") or h2.parent
    return None


def primary_content(soup: BeautifulSoup) -> Tag:
    return (
        soup.find(attrs={"aria-label": "Primary content"})
        or soup.find("main")
        or soup
    )


def classify_entry(texts: list[str], kind: str) -> dict:
    entry: dict = {}
    leftover: list[str] = []

    for text in texts:
        dates, duration = split_date_and_duration(text)
        if dates:
            entry["dates"] = dates
            if duration:
                entry["duration"] = duration
            continue
        if DURATION_RE.match(text):
            entry["duration"] = text
            continue
        if EMPLOYMENT_RE.fullmatch(text) or (
            EMPLOYMENT_RE.search(text) and len(text) < 40
        ):
            entry["employment_type"] = text
            continue
        if looks_like_location(text):
            entry["location"] = text
            continue
        leftover.append(text)

    if kind == "experience":
        if leftover:
            entry["title"] = leftover[0]
        if len(leftover) > 1:
            company, employment = split_company_line(leftover[1])
            entry["company"] = company
            if employment and "employment_type" not in entry:
                entry["employment_type"] = employment
        if len(leftover) > 2:
            entry["description"] = "\n".join(leftover[2:])
    elif kind == "role":
        if leftover:
            entry["title"] = leftover[0]
        if len(leftover) > 1:
            entry["description"] = "\n".join(leftover[1:])
    elif kind == "education":
        if leftover:
            entry["school"] = leftover[0]
        if len(leftover) > 1:
            entry["degree"] = leftover[1]
        if len(leftover) > 2:
            entry["description"] = "\n".join(leftover[2:])
    elif kind == "certification":
        if leftover:
            entry["name"] = leftover[0]
        if len(leftover) > 1:
            entry["issuer"] = leftover[1]
        if len(leftover) > 2:
            entry["details"] = "\n".join(leftover[2:])
    else:
        if leftover:
            entry["name"] = leftover[0]
        if len(leftover) > 1:
            entry["details"] = "\n".join(leftover[1:])

    return {key: value for key, value in entry.items() if value}


def parse_experience_item(item: Tag) -> dict | None:
    nested = item.find("ul")
    roles = nested.find_all("li", recursive=False) if nested else []
    if nested and roles:
        header_texts = []
        for p in item.find_all("p"):
            if nested in p.parents:
                continue
            text = tag_text(p)
            if text and not is_noise(text) and text not in header_texts:
                header_texts.append(text)

        company = None
        duration = None
        for text in header_texts:
            if DURATION_RE.match(text) or DATE_RANGE_RE.match(text):
                duration = text
            elif company is None:
                company = text

        parsed_roles = []
        for role in roles:
            parsed = classify_entry(paragraphs(role), "role")
            if company:
                parsed.setdefault("company", company)
            if parsed.get("title"):
                parsed_roles.append(parsed)

        if not parsed_roles:
            return None
        result = {"company": company, "roles": parsed_roles}
        if duration:
            result["duration"] = duration
        return result

    parsed = classify_entry(paragraphs(item), "experience")
    return parsed if parsed.get("title") or parsed.get("company") else None


def parse_named_items(section: Tag | None, kind: str) -> list[dict]:
    items = []
    for item in collection_items(section):
        if kind == "experience":
            parsed = parse_experience_item(item)
        elif kind == "skill":
            texts = paragraphs(item)
            parsed = {"name": texts[0]} if texts else None
        else:
            parsed = classify_entry(paragraphs(item), kind)
            if kind == "education" and not parsed.get("school"):
                parsed = None
            elif kind == "certification" and not parsed.get("name"):
                parsed = None
            if parsed and kind == "certification":
                cred = item.find(
                    "a",
                    href=True,
                    attrs={
                        "aria-label": lambda value: bool(value)
                        and "credential" in str(value).lower()
                    },
                )
                if cred:
                    parsed["credential_url"] = cred["href"]
        if parsed:
            items.append(parsed)
    return items


def parse_about(soup: Tag) -> str:
    section = find_by_component_suffix(soup, "About") or section_by_heading(soup, "About")
    if not section:
        return ""
    box = section.find(attrs={"data-testid": "expandable-text-box"})
    if box:
        return tag_text(box)
    texts = [
        text
        for text in paragraphs(section)
        if text.lower() not in {"about"}
    ]
    return "\n".join(texts)


def parse_topcard(soup: Tag) -> dict:
    card = find_by_component_suffix(soup, "Topcard") or soup
    data: dict[str, str] = {}

    name_el = card.find("h2") or card.find("h1") or soup.find("h1")
    name = tag_text(name_el)
    if not name:
        title = tag_text(soup.find("title"))
        name = title.split("|", 1)[0].strip()
    if name:
        data["name"] = name

    texts = [
        text
        for text in paragraphs(card)
        if text != name and not FOLLOWERS_RE.fullmatch(text)
    ]
    if texts:
        data["headline"] = texts[0]
    if len(texts) > 1 and not looks_like_location(texts[1]):
        data["current_company"] = texts[1]
    for text in texts:
        if looks_like_location(text):
            data["location"] = text
            break

    photo = card.find("img", src=lambda src: bool(src) and "profile-displayphoto" in src)
    if photo and photo.get("src"):
        data["photo_url"] = photo["src"]

    activity = section_by_heading(soup, "Activity")
    follower_text = tag_text(activity) if activity else tag_text(soup)
    followers = FOLLOWERS_RE.search(follower_text)
    if followers:
        data["followers"] = followers.group(1).replace(",", "")

    return data


SECTION_MAP = {
    "experience": ("profile_ExperienceTopLevelSection_", "Experience", "experience"),
    "education": ("profile_EducationTopLevelSection_", "Education", "education"),
    "certifications": ("profile_CertificationTopLevel_", "Licenses", "certification"),
    "skills": ("profile_Skills_", "Skills", "skill"),
}


def resolve_section(root: Tag, testid_prefix: str, heading: str) -> Tag | None:
    return find_by_testid_prefix(root, testid_prefix) or section_by_heading(root, heading)


def parse_profile_html(html: str, url: str = "", only: str | None = None) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    root = primary_content(soup)
    profile: dict = {"url": url}

    if only in (None, "topcard"):
        profile.update(parse_topcard(root))
    if only in (None, "about"):
        profile["about"] = parse_about(root)

    targets = (
        {only: SECTION_MAP[only]}
        if only in SECTION_MAP
        else SECTION_MAP
    )
    for key, (testid_prefix, heading, kind) in targets.items():
        section = resolve_section(root, testid_prefix, heading)
        if section is None and only == key:
            section = root
        profile[key] = parse_named_items(section, kind) if section else []
    return profile


def normalize_profile_url(url: str) -> str:
    match = re.search(r"(https?://(?:www\.)?linkedin\.com/in/[^/?#]+)", url.strip())
    if not match:
        raise ValueError(f"Not a LinkedIn profile URL: {url}")
    return match.group(1).rstrip("/")


def profile_filename(url: str) -> str:
    slug = normalize_profile_url(url).rsplit("/", 1)[-1]
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", slug).strip("._")
    return f"{slug or 'profile'}.json"


def save_profile_json(results: dict, url: str) -> Path:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        filename = profile_filename(url)
    except ValueError:
        name = str(results.get("name") or "profile")
        filename = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") + ".json"
    path = PROFILES_DIR / filename
    path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Saved profile to %s", path)
    return path


def merge_profile(base: dict, extra: dict, key: str) -> None:
    if key == "about":
        incoming = extra.get("about") or ""
        if incoming and len(incoming) >= len(base.get("about") or ""):
            base["about"] = incoming
        return
    incoming = extra.get(key) or []
    if incoming and len(incoming) >= len(base.get(key) or []):
        base[key] = incoming


class LinkedInParser:
    def __init__(self, headless: bool = False):
        self.options = webdriver.ChromeOptions()
        if headless:
            self.options.add_argument("--headless=new")
        self.options.add_argument("--no-sandbox")
        self.options.add_argument("--disable-dev-shm-usage")
        self.options.add_argument("--disable-gpu")
        self.options.add_argument("--start-maximized")
        self.options.add_argument("--disable-blink-features=AutomationControlled")
        self.options.add_argument(f"user-agent={USER_AGENT}")
        self.options.add_experimental_option("excludeSwitches", ["enable-automation"])
        self.options.add_experimental_option("useAutomationExtension", False)
        self.options.add_experimental_option(
            "prefs",
            {
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
            },
        )
        self.driver = None

    def start_driver(self) -> None:
        if self.driver:
            return
        try:
            self.driver = webdriver.Chrome(service=ChromeService(), options=self.options)
            self.driver.implicitly_wait(0)
            self.driver.set_page_load_timeout(60)
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                },
            )
        except WebDriverException as exc:
            logger.error("Error initializing ChromeDriver: %s", exc)
            raise

    def close_driver(self) -> None:
        if self.driver:
            self.driver.quit()
            self.driver = None

    def navigate_to_page(self, url: str) -> bool:
        try:
            if not self.driver:
                self.start_driver()
            self.driver.get(url)
            return True
        except Exception as exc:
            logger.error("Error navigating to %s: %s", url, exc)
            return False

    def get_page_content(self) -> BeautifulSoup:
        return BeautifulSoup(self.driver.page_source, "html.parser")

    def _dismiss_popups(self) -> None:
        selectors = [
            "button[aria-label='Dismiss']",
            "button[aria-label='Close']",
            "button[aria-label='Skip']",
            ".artdeco-modal__dismiss",
        ]
        for selector in selectors:
            for button in self.driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if button.is_displayed():
                        self.driver.execute_script("arguments[0].click();", button)
                except Exception:
                    continue

    def _scroll_page(self, pause: float = 1.2) -> None:
        last_height = 0
        for _ in range(12):
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(pause)
            height = self.driver.execute_script("return document.body.scrollHeight")
            if height == last_height:
                break
            last_height = height
        self.driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.5)

    def _expand_hidden_text(self) -> None:
        selectors = [
            '[data-testid="expandable-text-button"]',
            "button[aria-expanded='false']",
        ]
        for selector in selectors:
            for button in self.driver.find_elements(By.CSS_SELECTOR, selector):
                label = (button.get_attribute("aria-label") or button.text or "").lower()
                if selector.endswith("='false']") and "more" not in label:
                    continue
                try:
                    self.driver.execute_script("arguments[0].click();", button)
                    time.sleep(0.15)
                except Exception:
                    continue

    def _wait_for_profile(self, timeout: int = 20) -> None:
        WebDriverWait(self.driver, timeout).until(
            lambda driver: driver.find_elements(
                By.CSS_SELECTOR,
                '[componentkey*="Topcard"], h1, [data-testid^="profile_Experience"]',
            )
        )

    def _is_logged_in(self) -> bool:
        url = self.driver.current_url.lower()
        return not any(part in url for part in ("/login", "/checkpoint", "/authwall", "/challenge"))

    def _save_session(self) -> None:
        try:
            SESSION_PATH.write_text(json.dumps(self.driver.get_cookies(), indent=2))
        except Exception as exc:
            logger.warning("Could not save session cookies: %s", exc)

    def _load_session(self) -> bool:
        if not SESSION_PATH.exists():
            return False
        try:
            cookies = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
            self.navigate_to_page("https://www.linkedin.com/")
            time.sleep(1)
            for cookie in cookies:
                cookie.pop("sameSite", None)
                cookie.pop("expiry", None)
                try:
                    self.driver.add_cookie(cookie)
                except Exception:
                    continue
            self.navigate_to_page("https://www.linkedin.com/feed/")
            time.sleep(2)
            if self._is_logged_in():
                logger.info("Reused saved LinkedIn session")
                return True
        except Exception as exc:
            logger.warning("Could not reuse saved session: %s", exc)
        return False

    def _first_visible(self, selectors: list[tuple], timeout: int = 25):
        end = time.time() + timeout
        while time.time() < end:
            for by, value in selectors:
                for element in self.driver.find_elements(by, value):
                    try:
                        if element.is_displayed():
                            return element
                    except Exception:
                        continue
            time.sleep(0.4)
        raise TimeoutException(f"No visible element for {selectors[0]}")

    def _read_login_errors(self) -> str:
        soup = self.get_page_content()
        texts: list[str] = []
        for selector in LOGIN_ERROR_SELECTORS:
            for node in soup.select(selector):
                text = tag_text(node)
                if text and text not in texts:
                    texts.append(text)
        for node in soup.find_all(
            id=lambda value: bool(value) and str(value).startswith("error-for")
        ):
            text = tag_text(node)
            if text and text not in texts:
                texts.append(text)
        return " ".join(texts)

    def _uncheck_remember_me(self) -> None:
        selectors = [
            "#rememberMeOptIn-checkbox",
            "#rememberMeOptIn",
            "#rememberMe",
            "input[name='rememberMeOptIn']",
            "input[name='rememberMe']",
            "input[name='remember']",
            "input[id*='remember' i]",
            "input[name*='remember' i]",
            "form input[type='checkbox']",
        ]
        boxes = []
        for selector in selectors:
            boxes.extend(self.driver.find_elements(By.CSS_SELECTOR, selector))

        for label in self.driver.find_elements(
            By.XPATH,
            "//label[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
            "'abcdefghijklmnopqrstuvwxyz'), 'remember me') or "
            "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
            "'abcdefghijklmnopqrstuvwxyz'), 'keep me logged in')]",
        ):
            try:
                for_id = label.get_attribute("for")
                if for_id:
                    boxes.extend(self.driver.find_elements(By.ID, for_id))
                boxes.extend(label.find_elements(By.CSS_SELECTOR, "input[type='checkbox']"))
                label_text = (label.text or "").lower()
                if "remember" in label_text or "keep me logged" in label_text:
                    boxes.append(label)
            except Exception:
                continue

        for toggle in self.driver.find_elements(By.CSS_SELECTOR, "[role='checkbox']"):
            label = (
                toggle.get_attribute("aria-label") or toggle.text or ""
            ).lower()
            if "remember" in label or "keep me logged" in label:
                boxes.append(toggle)

        seen: set[str] = set()
        for box in boxes:
            try:
                key = box.id
                if key in seen:
                    continue
                seen.add(key)
                if box.tag_name.lower() == "input":
                    self.driver.execute_script(
                        """
                        const el = arguments[0];
                        if (!el) return;
                        if (el.checked) {
                            el.click();
                        }
                        if (el.checked) {
                            el.checked = false;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        """,
                        box,
                    )
                elif box.get_attribute("aria-checked") == "true":
                    self.driver.execute_script("arguments[0].click();", box)
                elif box.tag_name.lower() == "label":
                    self.driver.execute_script(
                        """
                        const label = arguments[0];
                        const box = label.control || document.getElementById(label.htmlFor);
                        if (box && box.checked) {
                            label.click();
                        }
                        if (box && box.checked) {
                            box.checked = false;
                            box.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        """,
                        box,
                    )
            except Exception:
                continue

    def _fill_input(self, element, value: str) -> None:
        element.click()
        element.send_keys(Keys.CONTROL + "a")
        element.send_keys(Keys.DELETE)
        element.clear()
        element.send_keys(value)

    def login(self, email: str, password: str) -> None:
        if not self.driver:
            self.start_driver()

        if self._load_session():
            return

        if not self.navigate_to_page("https://www.linkedin.com/login"):
            raise LinkedInAuthError("Could not open the LinkedIn login page.")

        time.sleep(2)
        self._dismiss_popups()
        for selector in (
            "#onetrust-accept-btn-handler",
            "button[action-type='ACCEPT']",
        ):
            for button in self.driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if button.is_displayed():
                        button.click()
                        time.sleep(0.5)
                except Exception:
                    continue

        try:
            email_el = self._first_visible(LOGIN_EMAIL_SELECTORS, timeout=30)
        except TimeoutException as exc:
            raise LinkedInAuthError(
                "LinkedIn login form did not load. "
                f"Current page: {self.driver.current_url}"
            ) from exc

        self._fill_input(email_el, email)
        time.sleep(0.4)

        try:
            password_el = self._first_visible(LOGIN_PASSWORD_SELECTORS, timeout=8)
        except TimeoutException:
            for button in self.driver.find_elements(By.CSS_SELECTOR, "button[type='submit']"):
                try:
                    if button.is_displayed():
                        button.click()
                        break
                except Exception:
                    continue
            try:
                password_el = self._first_visible(LOGIN_PASSWORD_SELECTORS, timeout=15)
            except TimeoutException as exc:
                raise LinkedInAuthError(
                    "LinkedIn password field did not load. "
                    f"Current page: {self.driver.current_url}"
                ) from exc

        self._fill_input(password_el, password)
        time.sleep(0.3)
        self._uncheck_remember_me()

        submitted = False
        for button in self.driver.find_elements(By.CSS_SELECTOR, "button[type='submit']"):
            try:
                if button.is_displayed():
                    button.click()
                    submitted = True
                    break
            except Exception:
                continue
        if not submitted:
            password_el.send_keys(Keys.RETURN)

        deadline = time.time() + 30
        while time.time() < deadline:
            error = self._read_login_errors()
            if error:
                raise LinkedInAuthError(error)
            url = self.driver.current_url.lower()
            if "/checkpoint" in url or "/challenge" in url:
                logger.warning(
                    "LinkedIn is asking for a security check. Complete it in the open browser."
                )
                WebDriverWait(self.driver, 180).until(lambda driver: self._is_logged_in())
                break
            if self._is_logged_in():
                break
            time.sleep(0.5)
        else:
            error = self._read_login_errors()
            raise LinkedInAuthError(
                error
                or f"LinkedIn login did not complete. Current page: {self.driver.current_url}"
            )

        if not self._is_logged_in():
            error = self._read_login_errors()
            raise LinkedInAuthError(
                error or "LinkedIn login failed. Check your email and password."
            )

        self._save_session()
        logger.info("Logged in to LinkedIn")

    def _parse_current_page(self, url: str, only: str | None = None) -> dict:
        self._dismiss_popups()
        self._scroll_page()
        self._expand_hidden_text()
        time.sleep(0.8)
        return parse_profile_html(self.driver.page_source, url, only=only)

    def _scrape_details(self, profile_url: str, slug: str) -> dict:
        details_url = f"{profile_url}/details/{slug}/"
        if not self.navigate_to_page(details_url):
            return {}
        time.sleep(2)
        if slug not in self.driver.current_url:
            return {}
        try:
            self._wait_for_profile(timeout=12)
        except TimeoutException:
            pass
        return self._parse_current_page(details_url, only=slug)

    def scrape_profile(self, profile_url: str, include_details: bool = True) -> dict:
        creds = saved_credentials()
        if not creds:
            raise LinkedInAuthError("No user logged in.")

        url = normalize_profile_url(profile_url)
        self.login(*creds)

        if not self.navigate_to_page(url):
            raise RuntimeError(f"Could not open {url}")

        try:
            self._wait_for_profile()
        except TimeoutException as exc:
            raise RuntimeError(f"Profile page did not load: {url}") from exc

        time.sleep(2)
        profile = self._parse_current_page(url)

        if include_details:
            for slug, key in (
                ("about", "about"),
                ("experience", "experience"),
                ("education", "education"),
                ("certifications", "certifications"),
                ("skills", "skills"),
            ):
                extra = self._scrape_details(url, slug)
                if extra:
                    merge_profile(profile, extra, key)

        profile["url"] = url
        return profile


def parse_html_file(path: str, url: str = "") -> dict:
    html = Path(path).read_text(encoding="utf-8")
    return parse_profile_html(html, url)


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse a LinkedIn profile page")
    parser.add_argument(
        "profile_url",
        nargs="?",
        default=None,
        help="LinkedIn profile URL (required unless using --login)",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Save or update your LinkedIn email and password",
    )
    parser.add_argument(
        "--html",
        help="Parse a saved HTML file instead of opening Chrome",
    )
    parser.add_argument("-o", "--output", help="Write JSON to this file")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chrome without a window (LinkedIn often blocks this)",
    )
    parser.add_argument(
        "--no-details",
        action="store_true",
        help="Only parse the main profile page, not /details/* pages",
    )
    args = parser.parse_args()

    if args.login:
        email, password = prompt_credentials()
        save_env_local(email, password)
        print("Login details saved successfully.")
        print_available_commands()
        return

    if not args.profile_url:
        parser.error("profile_url is required")

    if args.html:
        results = parse_html_file(args.html, args.profile_url)
    else:
        if not saved_credentials():
            print(
                "No user logged in. "
                f"Run `{cli_command()} --login` to save your LinkedIn login details, then try again.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        scraper = LinkedInParser(headless=args.headless)
        try:
            results = scraper.scrape_profile(
                args.profile_url, include_details=not args.no_details
            )
        except LinkedInAuthError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            print(
                f"Run `{cli_command()} --login` to update your login details and try again.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        finally:
            scraper.close_driver()

    rendered = json.dumps(results, indent=2, ensure_ascii=False)
    print(rendered)
    saved_to = save_profile_json(results, args.profile_url)
    print(f"Saved profile to {saved_to}")
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
        logger.info("Wrote %s", args.output)


if __name__ == "__main__":
    main()
