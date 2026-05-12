import asyncio
import logging
import os
import time

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from core.browser import get_browser
from core.msg_builder import build_message
from utils.checkpoint import (
    get_completed_targets,
    load_checkpoint,
    mark_target_completed,
    save_checkpoint,
)
from utils.config import get_config, get_userData
from utils.logger import setup_logger


config = get_config()
userData = get_userData()
logger = setup_logger(level=logging.DEBUG)


def get_run_deadline():
    seconds = os.getenv("DYSF_MAX_RUNTIME_SECONDS")
    if seconds is None:
        seconds = str(config.get("maxRunSeconds", ""))
    if not seconds and os.getenv("GITHUB_ACTIONS") == "true":
        seconds = "1080"
    if not seconds:
        return None

    try:
        seconds = int(seconds)
    except ValueError:
        logger.warning(f"DYSF_MAX_RUNTIME_SECONDS invalid: {seconds}")
        return None

    if seconds <= 0:
        return None
    return time.monotonic() + seconds


def has_time_left(deadline, reserve_seconds=60):
    return deadline is None or time.monotonic() + reserve_seconds < deadline


async def retry_operation(name, operation, retries=3, delay=2, *args, **kwargs):
    for attempt in range(retries):
        try:
            return await operation(*args, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"{name} failed, retry {attempt + 1}: {e}")
                await asyncio.sleep(delay)
            else:
                logger.error(f"{name} failed after {retries} retries: {e}")
                raise


async def scroll_and_select_user(page, username, targets, deadline=None):
    friends_tab_selector = (
        'xpath=//*[@id="sub-app"]//*[@role="tab" and normalize-space()="朋友私信"]'
    )
    target_selectors = [
        'xpath=//*[@id="sub-app"]//*[contains(@class, "semi-list-item-body")]',
        'xpath=//*[@id="sub-app"]//li[.//span[contains(@class, "item-header-name")]]',
        'xpath=//*[@id="sub-app"]//*[contains(@class, "semi-list-item")]',
    ]
    scrollable_friends_selector = (
        'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]/div/div/div[3]/div/div/div/ul/div'
    )
    no_more_selector = 'xpath=//div[contains(@class, "no-more-tip-ftdJnu")]'
    loading_selector = 'xpath=//div[contains(@class, "semi-spin")]'
    empty_selector = 'xpath=//*[@id="sub-app"]//*[contains(text(), "还没有收到私信")]'

    target_set = set(targets)
    logger.debug(f"账号 {username} 开始查找目标好友: {targets}")

    async def get_visible_target_elements(selector):
        elements = await page.locator(selector).all()
        visible_elements = []
        for element in elements:
            try:
                if await element.is_visible():
                    visible_elements.append(element)
            except Exception:
                continue
        return visible_elements

    await page.wait_for_selector(friends_tab_selector, timeout=60000)
    await page.locator(friends_tab_selector).click()
    logger.debug(f"账号 {username} 已进入朋友私信列表")

    active_target_selector = None
    for _ in range(60):
        if not has_time_left(deadline):
            logger.warning(f"账号 {username} 运行时间即将用尽，停止等待好友列表")
            return
        if await page.locator(empty_selector).count() > 0:
            logger.warning(f"账号 {username} 朋友私信为空，无法继续查找目标")
            return

        for selector in target_selectors:
            if await get_visible_target_elements(selector):
                active_target_selector = selector
                break
        if active_target_selector:
            break
        await asyncio.sleep(1)

    if not active_target_selector:
        logger.error(f"账号 {username} 等待好友列表超时")
        return

    first_items = await get_visible_target_elements(active_target_selector)
    if first_items:
        await first_items[0].click()
    await asyncio.sleep(0.5)

    seen_names = set()
    idle_scroll_count = 0
    max_idle_scroll_count = 8

    while target_set:
        if not has_time_left(deadline):
            logger.warning(f"账号 {username} 运行时间即将用尽，保存进度后停止搜索")
            break

        target_elements = await get_visible_target_elements(active_target_selector)
        found_new_name = False
        selected_target = None

        for element in target_elements:
            try:
                span = element.locator(
                    'xpath=.//span[contains(@class, "item-header-name-")]'
                ).first
                target_name = (await span.inner_text(timeout=1000)).strip()
            except Exception:
                continue

            if target_name and target_name not in seen_names:
                seen_names.add(target_name)
                found_new_name = True
                logger.debug(f"账号 {username} 找到好友 {target_name}")

            if target_name in target_set:
                await element.click()
                selected_target = target_name
                logger.info(f"账号 {username} 选中目标好友 {target_name}")
                break

        if selected_target:
            yield selected_target
            target_set.discard(selected_target)
            continue

        if found_new_name:
            idle_scroll_count = 0
        else:
            idle_scroll_count += 1
            if idle_scroll_count >= max_idle_scroll_count:
                logger.warning(
                    f"账号 {username} 连续 {max_idle_scroll_count} 次滚动没有新好友，"
                    f"剩余未找到目标: {sorted(target_set)}"
                )
                break

        if await page.locator(no_more_selector).count() > 0:
            logger.warning(f"账号 {username} 已到列表底部，剩余未找到目标: {sorted(target_set)}")
            break

        if await page.locator(loading_selector).count() > 0:
            await asyncio.sleep(0.5)

        scrollable_element = await page.locator(
            scrollable_friends_selector
        ).element_handle()
        if not scrollable_element:
            logger.error(f"账号 {username} 未找到滚动容器")
            break

        await page.evaluate("(element) => element.scrollTop += 1600", scrollable_element)
        await asyncio.sleep(0.5)


async def wait_for_chat_input(page, account_name, target_name):
    chat_input_selectors = [
        'xpath=//*[@id="sub-app"]//*[@contenteditable="true"]',
        'xpath=//div[@contenteditable="true"]',
        'xpath=//div[contains(@class, "chat-input")]',
        "xpath=//textarea",
    ]

    last_error = None
    for selector in chat_input_selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=15000)
            logger.debug(
                f"账号 {account_name} 给好友 {target_name} 找到输入框: {selector}"
            )
            return locator
        except PlaywrightTimeoutError as e:
            last_error = e

    logger.error(f"账号 {account_name} 给好友 {target_name} 未找到可见私信输入框")
    if last_error:
        raise last_error
    raise RuntimeError("未找到可见私信输入框")


async def send_message(chat_input, message):
    try:
        await chat_input.fill(message)
        return
    except Exception:
        message_lines = message.split("\n")
        for index, line in enumerate(message_lines):
            await chat_input.type(line, delay=0)
            if index != len(message_lines) - 1:
                await chat_input.press("Shift+Enter")


async def do_user_task(
    browser,
    unique_id,
    username,
    cookies,
    targets,
    semaphore,
    checkpoint_state,
    checkpoint_lock,
    deadline=None,
):
    async with semaphore:
        context = await browser.new_context()
        context.set_default_navigation_timeout(60000)
        context.set_default_timeout(60000)
        try:
            page = await context.new_page()
            await retry_operation(
                "打开抖音创作者中心",
                page.goto,
                retries=3,
                delay=5,
                url="https://creator.douyin.com/",
            )
            await context.add_cookies(cookies)
            await retry_operation(
                "导航到消息页面",
                page.goto,
                retries=3,
                delay=5,
                url="https://creator.douyin.com/creator-micro/data/following/chat",
            )

            logger.info(f"账号 {username} 开始发送消息，待处理目标数: {len(targets)}")
            async for target_name in scroll_and_select_user(
                page, username, targets, deadline
            ):
                if not has_time_left(deadline):
                    logger.warning(f"账号 {username} 运行时间即将用尽，停止发送新目标")
                    break

                chat_input = await wait_for_chat_input(page, username, target_name)
                await chat_input.click()

                message = build_message()
                await send_message(chat_input, message)
                logger.debug(f"账号 {username} 准备发送给 {target_name}:\n\t{message}")
                await chat_input.press("Enter")

                async with checkpoint_lock:
                    mark_target_completed(
                        checkpoint_state, unique_id, username, target_name, message
                    )
                    save_checkpoint(checkpoint_state)

                logger.info(f"账号 {username} 给好友 {target_name} 发送完成，已写入 checkpoint")
                await asyncio.sleep(0.5)
        finally:
            await context.close()


async def runTasks():
    checkpoint_state = load_checkpoint()
    checkpoint_lock = asyncio.Lock()
    deadline = get_run_deadline()
    if deadline:
        logger.info("已启用运行时间预算，临近超时会自动停止并保留 checkpoint")

    logger.info("开始执行任务，当前配置如下:")
    logger.info(f"多任务模式: {config['multiTask']}, 任务数量: {config['taskCount']}")
    logger.info(f"消息模板: {config['messageTemplate']}")
    logger.info(f"一言类型: {config['hitokotoTypes']}")

    semaphore = asyncio.Semaphore(config["taskCount"] if config["multiTask"] else 1)
    jobs = []

    for user in userData:
        unique_id = user["unique_id"]
        username = user.get("username", "未知用户")
        targets = user["targets"]
        completed = get_completed_targets(checkpoint_state, unique_id)
        pending_targets = [target for target in targets if target not in completed]

        logger.info(
            f"用户: {username}, 总目标: {len(targets)}, "
            f"今日已完成: {len(completed)}, 本次待处理: {len(pending_targets)}"
        )

        if not pending_targets:
            logger.info(f"用户 {username} 今日目标已全部完成，跳过")
            continue

        if not has_time_left(deadline):
            logger.warning("运行时间即将用尽，停止创建新账号任务")
            break

        jobs.append(
            {
                "unique_id": unique_id,
                "username": username,
                "cookies": user["cookies"],
                "targets": pending_targets,
            }
        )

    if not jobs:
        logger.info("没有需要执行的目标")
        save_checkpoint(checkpoint_state)
        return

    playwright, browser = await get_browser()
    try:
        tasks = [
            do_user_task(
                browser=browser,
                unique_id=job["unique_id"],
                username=job["username"],
                cookies=job["cookies"],
                targets=job["targets"],
                semaphore=semaphore,
                checkpoint_state=checkpoint_state,
                checkpoint_lock=checkpoint_lock,
                deadline=deadline,
            )
            for job in jobs
        ]
        await asyncio.gather(*tasks)
    finally:
        await browser.close()
        await playwright.stop()
        save_checkpoint(checkpoint_state)
