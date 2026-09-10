# -*- coding: utf-8 -*-

import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


URL = "https://zakup.sk.kz/"
KEYWORD = "картридж"

OUT = Path("output")
OUT.mkdir(exist_ok=True)


def main():
    print("=" * 60)
    print("SAMRUK KAZYNA - GITHUB HEADLESS TEST")
    print("=" * 60)

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=ru-RU")

    driver = webdriver.Chrome(options=options)

    try:
        print("OPEN:", URL)
        driver.get(URL)

        wait = WebDriverWait(driver, 40)
        wait.until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        print("TITLE:", driver.title)
        print("URL:", driver.current_url)

        inputs = driver.find_elements(By.TAG_NAME, "input")

        search_box = None

        for item in inputs:
            placeholder = item.get_attribute("placeholder") or ""
            if (
                "Слово для поиска" in placeholder
                or "номер закупки" in placeholder
            ):
                search_box = item
                break

        if search_box is None:
            print("ERROR: search input not found")
            print("INPUTS FOUND:")

            for item in inputs:
                print(" -", item.get_attribute("placeholder"))

            raise RuntimeError("Samruk search input not found")

        print("SEARCH INPUT FOUND")
        print("KEYWORD:", KEYWORD)

        search_box.clear()
        search_box.send_keys(KEYWORD)

        try:
            button = wait.until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//button[contains(normalize-space(.),'Найти')]"
                    )
                )
            )
            button.click()
        except Exception:
            print("Search button not clicked - trying ENTER")
            search_box.send_keys(Keys.ENTER)

        print("SEARCH STARTED")

        time.sleep(8)

        body_text = driver.find_element(By.TAG_NAME, "body").text

        print("RESULT URL:", driver.current_url)
        print("PAGE TEXT LENGTH:", len(body_text))

        OUT.joinpath("samruk_test.html").write_text(
            driver.page_source,
            encoding="utf-8"
        )

        OUT.joinpath("samruk_test.txt").write_text(
            body_text,
            encoding="utf-8"
        )

        print("")
        print("LINES WITH KEYWORD:")
        found = 0

        for line in body_text.splitlines():
            if KEYWORD.lower() in line.lower():
                print(line[:500])
                found += 1
                if found >= 20:
                    break

        print("")
        print("KEYWORD LINES:", found)

        if "По вашему запросу ничего не найдено" in body_text:
            print("SAMRUK RESULT: no matches shown")
        else:
            print("SAMRUK RESULT: page responded to search")

        print("SUCCESS: Samruk browser test completed")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
