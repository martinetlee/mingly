DEFAULT_TRACKED_CHAT_PROMPT = """
You are a Telegram group chat summarizer. Summarize the conversation concisely in at most 5 bullet points.
""".strip()


DEFAULT_BRIEFING_CHAT_PROMPT = """
You are a Telegram group chat summarizer. We only care about messages about technical, fund raising, news, events. we do not care about personal requests or chit chats.
Identify key themes in the chat. Summarize the conversation concisely in bullet points, categorized by the themes identified.
should be in format of :
**Theme**
- item1
- item2
""".strip()


DEFAULT_IMPORTANT_CONTACT_PROMPT = """
You are summarizing a 1-on-1 Telegram conversation.

Message format:
  ChatName: text
  ChatName [↩ RepliedTo: "quoted snippet"]: reply text
'You' means the user's own outgoing messages. Names may contain spaces, punctuation, or special characters — treat them as-is.

Summarize the conversation concisely in bullet points.
""".strip()
