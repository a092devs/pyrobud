import asyncio
import string
from datetime import timezone, timedelta

import telethon as tg
import nostril

from pyrobud import command, module, util


class AntibotModule(module.Module):
    name = "Antibot"

    suspicious_keywords = ["invest", "profit", "binance", "binanse", "bitcoin", "testnet", "bitmex"]

    suspicious_entities = [
        tg.types.MessageEntityUrl,
        tg.types.MessageEntityTextUrl,
        tg.types.MessageEntityEmail,
        tg.types.MessageEntityPhone,
    ]

    suspicious_first_names = [
        "announcement",
        "info",
        "urgent",
        "limited",
        "holiday",
        "verified",
        "solidified",
        "recommended",
        "temporarily",
    ]

    async def on_load(self):
        self.db = self.bot.get_db("antibot")
        self.group_db = self.db.prefixed_db("groups.")
        self.user_db = self.db.prefixed_db("users.")

        # Migrate message tracking start times to the new per-group format
        fmsg_start_time = await self.db.get("first_msg_start_time")
        if fmsg_start_time is not None:
            self.log.info("Migrating message tracking start times to the new per-group format")

            async for key, value in self.group_db:
                if key.endswith(".enabled") and value:
                    await self.group_db.put(key.replace(".enabled", ".enable_time"), fmsg_start_time)

            await self.db.delete("first_msg_start_time")

    def msg_has_suspicious_entity(self, msg):
        if not msg.entities:
            return False

        # Messages containing certain entities are more likely to be spam
        for entity in msg.entities:
            if entity.__class__ in self.__class__.suspicious_entities:
                return True

        return False

    def msg_has_suspicious_keyword(self, msg):
        if not msg.raw_text:
            return False

        # Many spam messages mention certain keywords, such as cryptocurrency exchanges
        l_text = msg.raw_text.lower()
        for kw in self.__class__.suspicious_keywords:
            if kw in l_text:
                return True

        return False

    def msg_content_suspicious(self, msg):
        # Consolidate message content checks
        return self.msg_has_suspicious_entity(msg) or self.msg_has_suspicious_keyword(msg)

    def msg_data_is_suspicious(self, msg):
        incoming = not msg.out
        has_date = msg.date
        forwarded = msg.forward

        # Message *could* be suspicious if we didn't send it
        # Check for a date to exonerate empty messages
        if incoming and has_date:
            # Lazily evalulate suspicious content as it is more expensive
            return (forwarded and msg.photo) or self.msg_content_suspicious(msg)

        return False

    async def msg_is_suspicious(self, msg):
        # Check if the data in the message is suspicious
        if not self.msg_data_is_suspicious(msg):
            return False

        # Load group-specific user information
        chat = await msg.get_chat()
        sender = await msg.get_sender()
        ch_participant = await self.bot.client(tg.tl.functions.channels.GetParticipantRequest(chat, sender))
        participant = ch_participant.participant

        # Exempt the group creator
        if isinstance(participant, tg.tl.types.ChannelParticipantCreator):
            return False

        delta = msg.date - participant.date
        if delta.total_seconds() <= await self.db.get("threshold_time", 30):
            # Suspicious message was sent shortly after joining
            return True

        join_time_sec = int(participant.date.replace(tzinfo=timezone.utc).timestamp())
        if join_time_sec > await self.group_db.get(f"{msg.chat_id}.enable_time"):
            # We started tracking first messages in this group before the user
            # joined, so we can run the first message check
            if not await self.user_db.get(f"{sender.id}.has_spoken_in_{msg.chat_id}", False):
                # Suspicious message was the user's first message in this group
                return True

        # Allow this message
        return False

    async def profile_check_nonsense(self, user):
        # Users with unpronounceable ~12-character-long usernames that have the
        # first character capitalized and lack a profile (avatar/bio) tend to
        # be spambots
        if user.username and 10 <= len(user.username) <= 12:
            # Exonerate the user if the first character isn't capital A-Z or
            # subsequent characters aren't lowercase a-z
            if user.username[0] not in string.ascii_uppercase:
                return False
            if not all(c in string.ascii_lowercase for c in user.username[1:]):
                return False

            # Exonerate users with an avatar
            if user.photo:
                return False

            # Exonerate users who have a bio set
            full_user = await self.bot.client(tg.tl.functions.users.GetFullUserRequest(user))
            if full_user.about:
                return False

            # Check whether the username is pronounceable
            try:
                if not nostril.nonsense(user.username):
                    return False
            except ValueError as e:
                # Nostril failed to process the string; log a warning and
                # exonerate the user
                self.log.warn(f"Nostril's nonsense word checker failed to process name '{user.username}'", exc_info=e)
                return True

            # All conditions match; mark this user as suspicious
            return True

        # Allow this user
        return False

    def profile_check_crypto(self, user):
        # Many cryptocurrency spammers have attention-grabbing names that no
        # legitimate users would actually use as a name
        if user.first_name.lower() in self.__class__.suspicious_first_names:
            # Suspicious first name
            return True

        # Many cryptocurrency spammers also have Telegram invite links in their
        # first or last names
        if "t.me" in user.first_name or (user.last_name and "t.me" in user.last_name):
            # Suspicious name
            return True

        # Allow this user
        return False

    async def user_is_suspicious(self, user):
        # 10-12 character nonsense names without profile info
        if await self.profile_check_nonsense(user):
            return True

        # Cryptocurrency spammers with attention-grabbing names
        if self.profile_check_crypto(user):
            return True

        # No profile checks matched; exonerate this user
        return False

    async def take_action(self, event, user):
        # Wait a bit for welcome bots to react
        await asyncio.sleep(1)

        # Delete all of the sender's messages
        chat = await event.get_chat()
        request = tg.tl.functions.channels.DeleteUserHistoryRequest(chat, user)
        await self.bot.client(request)

        # Kick the sender
        await self.bot.client.kick_participant(chat, user)

        # Log the event
        self.log.info(f'Kicked spambot with ID {user.id} in group "{chat.title}"')
        await event.reply(f"❯❯ **Kicked auto-detected spambot** with ID `{user.id}`", schedule=timedelta(seconds=10))
        self.bot.dispatch_event_nowait("stat_event", "spambots_banned")

        # Delete the spam message just in case
        await event.delete()

    async def is_enabled(self, event):
        return event.is_group and await self.group_db.get(f"{event.chat_id}.enabled", False)

    async def on_message(self, msg):
        # Only run in groups where antibot is enabled
        if await self.is_enabled(msg):
            if await self.msg_is_suspicious(msg):
                # This is most likely a spambot, take action against the user
                user = await msg.get_sender()
                await self.take_action(msg, user)
            else:
                await self.user_db.put(f"{msg.sender_id}.has_spoken_in_{msg.chat_id}", True)

    async def clear_group(self, group_id):
        async for key, _ in self.group_db.iterator(prefix=f"{group_id}."):
            await self.group_db.delete(key)

        async for key, _ in self.user_db:
            if key.endswith(f".has_spoken_in_{group_id}"):
                await self.user_db.delete(key)

    async def on_chat_action(self, action):
        # Remove has-spoken-in flag for departing users
        if action.user_left and await self.is_enabled(action):
            await self.user_db.delete(f"{action.user_id}.has_spoken_in_{action.chat_id}")

            # Clean up antibot data if we left the group
            if action.user_id == self.bot.uid:
                self.log.info(f"Cleaning up settings for group {action.chat_id}")
                await self.clear_group(action.chat_id)

            return

        # Only filter new users
        if not action.user_added and not action.user_joined:
            return

        # Only act in groups where this is enabled
        if not await self.is_enabled(action):
            return

        # Fetch the user's data and run checks
        user = await action.get_user()
        if await self.user_is_suspicious(user):
            # This is most likely a spambot, take action against the user
            await self.take_action(action, user)

    @command.desc("Toggle the antibot auto-moderation feature in this group")
    async def cmd_antibot(self, msg):
        if not msg.is_group:
            return "__Antibot can only be used in groups.__"

        if not msg.is_channel:
            return "__Please convert this group to a supergroup in order to enable antibot.__"

        state = not await self.group_db.get(f"{msg.chat_id}.enabled", False)

        if state:
            # Check for required permissions
            chat = await msg.get_chat()
            ch_participant = await self.bot.client(tg.tl.functions.channels.GetParticipantRequest(chat, "me"))
            ptcp = ch_participant.participant

            if isinstance(ptcp, tg.types.ChannelParticipantCreator):
                # Group creator always has all permissions
                pass
            elif isinstance(ptcp, tg.types.ChannelParticipantAdmin):
                # Check for the required admin permissions
                if not ptcp.admin_rights.delete_messages or not ptcp.admin_rights.ban_users:
                    return "__Antibot requires the **Delete Messages** and **Ban users** permissions.__"
            else:
                return "__I must be an admin with the **Delete Messages** and **Ban users** permissions for antibot to work.__"

            await self.group_db.put(f"{msg.chat_id}.enabled", True)
            await self.group_db.put(f"{msg.chat_id}.enable_time", util.time.sec())
        else:
            await self.clear_group(msg.chat_id)

        status = "enabled" if state else "disabled"
        return f"Antibot is now **{status}** in this group."
