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


def save_page(driver, prefix):
    OUT.joinpath(prefix + ".html").write_text(
        driver.page_source,
        encoding="utf-8"
    )

    body_text = driver.find_element(By.TAG_NAME, "body").text

    OUT.joinpath(prefix + ".txt").write_text(
        body_text,
        encoding="utf-8"
    )

    try:
        driver.save_screenshot(
            str(OUT / (prefix + ".png"))
        )
    except Exception as e:
        print("SCREENSHOT ERROR:", e)

    return body_text


def print_page_elements(driver):
    print("")
    print("=== INPUTS FOUND ===")

    inputs = driver.find_elements(By.TAG_NAME, "input")

    for n, item in enumerate(inputs, 1):
        print(
            "INPUT", n,
            "type=", repr(item.get_attribute("type")),
            "placeholder=", repr(item.get_attribute("placeholder")),
            "name=", repr(item.get_attribute("name")),
            "id=", repr(item.get_attribute("id")),
            "aria-label=", repr(item.get_attribute("aria-label"))
        )

    print("")
    print("=== TEXTAREAS FOUND ===")

    textareas = driver.find_elements(By.TAG_NAME, "textarea")

    for n, item in enumerate(textareas, 1):
        print(
            "TEXTAREA", n,
            "placeholder=", repr(item.get_attribute("placeholder")),
            "name=", repr(item.get_attribute("name")),
            "id=", repr(item.get_attribute("id")),
            "aria-label=", repr(item.get_attribute("aria-label"))
        )

    print("")
    print("=== BUTTONS FOUND ===")

    buttons = driver.find_elements(By.TAG_NAME, "button")

    for n, button in enumerate(buttons, 1):
        print(
            "BUTTON", n,
            "text=", repr(button.text),
            "type=", repr(button.get_attribute("type")),
            "title=", repr(button.get_attribute("title")),
            "aria-label=", repr(button.get_attribute("aria-label"))
        )

    return inputs


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
            EC.presence_of_element_located(
                (By.TAG_NAME, "body")
            )
        )

        time.sleep(6)

        print("TITLE:", driver.title)
        print("URL:", driver.current_url)

        body_text = save_page(
            driver,
            "samruk_before_search"
        )

        inputs = print_page_elements(driver)

        search_box = None

        for item in inputs:
            placeholder = (
                item.get_attribute("placeholder") or ""
            )

            if (
                "Слово для поиска" in placeholder
                or "номер закупки" in placeholder
            ):
                search_box = item
                break

        if search_box is None:
            print("")
            print("ERROR: real Samruk search input not found")
            print("Page files saved to output/")
            print("We do NOT use 'Ваш вопрос...' as tender search.")
            raise RuntimeError(
                "Samruk tender search input not found"
            )

        print("")
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
            print(
                "Search button not clicked - trying ENTER"
            )

            search_box.send_keys(Keys.ENTER)

        print("SEARCH STARTED")

        time.sleep(8)

        body_text = save_page(
            driver,
            "samruk_after_search"
        )

        print("")
        print("RESULT URL:", driver.current_url)
        print("PAGE TEXT LENGTH:", len(body_text))

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
            print(
                "SAMRUK RESULT: page responded to search"
            )

        print("")
        print(
            "SUCCESS: Samruk browser test completed"
        )

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
