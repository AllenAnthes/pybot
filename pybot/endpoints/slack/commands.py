import functools
import logging
import random

from sirbot import SirBot
from sirbot.plugins.slack import SlackPlugin
from slack import methods
from slack.commands import Command
from slack.exceptions import SlackAPIError

from pybot.endpoints.slack.message_templates.commands import (
    ticket_dialog,
    mentor_request_attachments,
)
from pybot.endpoints.slack.utils import (
    PYBACK_HOST,
    PYBACK_PORT,
    PYBACK_TOKEN,
    MODERATOR_CHANNEL,
)
from pybot.endpoints.slack.utils.action_messages import not_claimed_attachment
from pybot.endpoints.slack.utils.command_utils import (
    get_slash_here_messages,
    get_slash_repeat_messages,
)
from pybot.endpoints.slack.utils.slash_lunch import LunchCommand

logger = logging.getLogger(__name__)


def create_endpoints(plugin: SlackPlugin):
    plugin.on_command("/here", slash_here, wait=False)
    plugin.on_command("/lunch", slash_lunch, wait=False)
    plugin.on_command("/repeat", slash_repeat, wait=False)
    plugin.on_command("/report", slash_report, wait=False)
    plugin.on_command("/ticket", slash_ticket, wait=False)
    plugin.on_command("/roll", slash_roll, wait=False)
    # plugin.on_command('/mentor', slash_mentor, wait=False)


def catch_slack_error(func):
    """
    Decorator for wrapping/catching exceptions thrown by
    the slack client and displaying an error to the user.

    Only necessary (for now) for functions that post messages to
    slack channels
    """

    @functools.wraps(func)
    async def handler(command: Command, app: SirBot, *args, **kwargs):
        try:
            await func(command, app, *args, **kwargs)

        except SlackAPIError:
            channel_id = command["channel_id"]
            slash_command = command["command"]
            slack_id = command["user_id"]
            slack = app["plugins"]["slack"]

            await slack.api.query(
                methods.CHAT_POST_EPHEMERAL,
                dict(
                    user=slack_id,
                    channel=slack_id,
                    as_user=True,
                    text=(
                        f"Could not post result of `{slash_command}` "
                        f"to channel <#{channel_id}>"
                    ),
                ),
            )

    return handler


@catch_slack_error
async def slash_mentor(command: Command, app: SirBot):
    airtable = app.plugins["airtable"].api
    services = await airtable.get_all_records("Services", "Name")
    mentors = await airtable.get_all_records("Mentors", "Full Name")
    skillsets = await airtable.get_all_records("Skillsets", "Skillset")

    dialog = mentor_request_attachments(services, mentors, skillsets)

    response = {"attachments": dialog, "channel": command["user_id"], "as_user": True}
    await app.plugins["slack"].api.query(methods.CHAT_POST_MESSAGE, response)


@catch_slack_error
async def slash_ticket(command: Command, app: SirBot):
    trigger_id = command["trigger_id"]
    user_id = command["user_id"]
    logger.warning(command["text"])

    user_info = await app.plugins["slack"].api.query(
        methods.USERS_INFO, {"user": user_id}
    )
    clicker_email = user_info["user"]["profile"]["email"]

    response = {
        "trigger_id": trigger_id,
        "dialog": ticket_dialog(clicker_email, command["text"]),
    }

    await app.plugins["slack"].api.query(methods.DIALOG_OPEN, response)


@catch_slack_error
async def slash_report(command: Command, app: SirBot):
    """
    Sends text supplied with the /report command to the moderators channel along
    with a button to claim the issue
    """
    slack_id = command["user_id"]
    text = command["text"]

    slack = app["plugins"]["slack"].api

    message = f"<@{slack_id}> sent report: {text}"

    response = {
        "text": message,
        "channel": MODERATOR_CHANNEL,
        "attachments": [not_claimed_attachment()],
    }

    await slack.query(methods.CHAT_POST_MESSAGE, response)


@catch_slack_error
async def slash_here(command: Command, app: SirBot):
    """
    /here allows admins to give non-admins the ability to use @here-esque functionality for specific channels.
    Queries pyback to determine if user is authorized
    """
    channel_id = command["channel_id"]
    slack_id = command["user_id"]
    slack = app["plugins"]["slack"].api

    params = {"slack_id": slack_id, "channel_id": channel_id}
    headers = {"Authorization": f"Token {PYBACK_TOKEN}"}

    logger.debug(f"/here params: {params}, /here headers {headers}")
    async with app.http_session.get(
        f"http://{PYBACK_HOST}:{PYBACK_PORT}/api/mods/", params=params, headers=headers
    ) as r:

        logger.debug(f"pyback response status: {r.status}")
        if r.status >= 400:
            return

        response = await r.json()
        logger.debug(f"pyback response: {response}")
        if not len(response):
            return

    message, member_list = await get_slash_here_messages(
        slack_id, channel_id, slack, command["text"]
    )

    response = await slack.query(
        methods.CHAT_POST_MESSAGE, {"channel": channel_id, "text": message}
    )
    timestamp = response["ts"]
    await slack.query(
        methods.CHAT_POST_MESSAGE,
        {"channel": channel_id, "text": member_list, "thread_ts": timestamp},
    )


@catch_slack_error
async def slash_lunch(command: Command, app: SirBot):
    """
    Provides the user with a random restaurant in their area.
    """
    logger.debug(command)
    lunch = LunchCommand(
        command["channel_id"],
        command["user_id"],
        command.get("text"),
        command["user_name"],
    )

    slack = app["plugins"]["slack"].api

    request = lunch.get_yelp_request()
    async with app.http_session.get(**request) as r:
        r.raise_for_status()
        message_params = lunch.select_random_lunch(await r.json())

        await slack.query(methods.CHAT_POST_EPHEMERAL, message_params)


@catch_slack_error
async def slash_repeat(command: Command, app: SirBot):
    logger.info(f"repeat command data incoming {command}")
    channel_id = command["channel_id"]
    slack_id = command["user_id"]
    slack = app["plugins"]["slack"].api

    method_type, message = get_slash_repeat_messages(
        slack_id, channel_id, command["text"]
    )

    await slack.query(method_type, message)


@catch_slack_error
async def slash_roll(command: Command, app: SirBot):
    slack_id = command["user_id"]
    channel_id = command["channel_id"]
    text = command["text"]

    slack = app["plugins"]["slack"].api

    # parse the type of die and number to roll
    try:
        text = text.lower()
        numdice, typedice = text.split("d")
        numdice = int(numdice)
        typedice = int(typedice)
        if numdice <= 0 or numdice > 10:
            raise ValueError
        if typedice <= 0 or typedice > 20:
            raise ValueError
    except ValueError:
        logger.debug("invalid input to roll: %s", text)
        response = dict(
            user=slack_id,
            channel=channel_id,
            text=(
                "Sorry, I didn't understand your input. "
                "Should be XDYY where X is the number of dice, and YY is the number of sides"
            ),
        )
        await slack.query(methods.CHAT_POST_EPHEMERAL, response)

        return
    dice = []
    for _ in range(0, numdice):
        dice.append(random.randint(1, typedice + 1))

    message = f"<@{slack_id}> Rolled {numdice} D{typedice}: {dice}"
    await slack.query(
        methods.CHAT_POST_MESSAGE, {"channel": channel_id, "text": message}
    )
