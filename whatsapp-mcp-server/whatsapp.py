import sqlite3
import sys
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List, Tuple
import os.path
import requests
import json
import audio

MESSAGES_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'messages.db')
WHATSAPP_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'whatsapp.db')
WHATSAPP_API_BASE_URL = "http://localhost:8080/api"
# (connect, read): мост локальный, поэтому соединение ждём недолго, а ответа
# на отправку или скачивание медиа - сколько нужно на передачу файла.
BRIDGE_TIMEOUT = (5, 30)
BRIDGE_MEDIA_TIMEOUT = (5, 300)


def _connect_messages_db() -> sqlite3.Connection:
    """Open messages.db and ATTACH whatsapp.db as 'wa' so we can resolve LIDs and contacts."""
    conn = sqlite3.connect(MESSAGES_DB_PATH)
    if os.path.isfile(WHATSAPP_DB_PATH):
        conn.execute(f"ATTACH DATABASE '{WHATSAPP_DB_PATH}' AS wa")
    return conn


def _normalize_phone(phone: str) -> str:
    """Strip everything except digits — phone JIDs and the lid_map.pn column store digits only."""
    return ''.join(c for c in (phone or '') if c.isdigit())


def _jid_candidates_for_phone(conn: sqlite3.Connection, phone: str) -> List[str]:
    """Return every JID this phone could appear under: the phone JID plus any linked LID JIDs."""
    digits = _normalize_phone(phone)
    if not digits:
        return []
    candidates = [f"{digits}@s.whatsapp.net"]
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT lid FROM wa.whatsmeow_lid_map WHERE pn = ?", (digits,))
        for (lid,) in cursor.fetchall():
            candidates.append(f"{lid}@lid")
    except sqlite3.Error:
        pass
    return candidates


def _user_part_candidates_for_phone(conn: sqlite3.Connection, phone: str) -> List[str]:
    """Same as _jid_candidates_for_phone but returns just the user part (no @suffix) — that's what messages.sender stores."""
    return [jid.split('@')[0] for jid in _jid_candidates_for_phone(conn, phone)]


def _jid_aliases_for_identifier(conn: sqlite3.Connection, identifier: str) -> List[str]:
    """Return phone/LID JIDs that may represent the same direct contact."""
    raw = (identifier or '').strip()
    if not raw:
        return []

    user_part = raw.split('@')[0] if '@' in raw else _normalize_phone(raw) or raw
    aliases: List[str] = []

    def add(jid: Optional[str]) -> None:
        if jid and jid not in aliases:
            aliases.append(jid)

    if '@' in raw:
        add(raw)

    if user_part.isdigit():
        add(f"{user_part}@s.whatsapp.net")

    try:
        if raw.endswith('@lid') or (user_part.isdigit() and len(user_part) > 13):
            add(f"{user_part}@lid")
            cursor = conn.cursor()
            cursor.execute("SELECT pn FROM wa.whatsmeow_lid_map WHERE lid = ?", (user_part,))
            row = cursor.fetchone()
            if row:
                add(f"{row[0]}@s.whatsapp.net")
        elif user_part.isdigit():
            cursor = conn.cursor()
            cursor.execute("SELECT lid FROM wa.whatsmeow_lid_map WHERE pn = ?", (user_part,))
            for (lid,) in cursor.fetchall():
                add(f"{lid}@lid")
    except sqlite3.Error:
        pass

    return aliases


def _user_part_aliases_for_identifier(conn: sqlite3.Connection, identifier: str) -> List[str]:
    """Return sender-column aliases for a phone number, phone JID, or LID JID."""
    return [jid.split('@')[0] for jid in _jid_aliases_for_identifier(conn, identifier)]


@dataclass
class Message:
    timestamp: datetime
    sender: str
    content: str
    is_from_me: bool
    chat_jid: str
    id: str
    chat_name: Optional[str] = None
    media_type: Optional[str] = None
    reply_to: Optional[str] = None
    reply_sender: Optional[str] = None
    mentions: Optional[List[str]] = None

# Колонки, которыми читаются сообщения: один список на все запросы, чтобы
# индексы в разборе строки не разъезжались с SELECT.
MESSAGE_COLUMNS = (
    "messages.timestamp, messages.sender, chats.name, messages.content, "
    "messages.is_from_me, chats.jid, messages.id, messages.media_type, "
    "messages.reply_to, messages.reply_sender, messages.mentions"
)


def mentions_from_row(raw: Optional[str]) -> Optional[List[str]]:
    """Упомянутые JID: мост пишет их JSON-массивом, старые строки пусты."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) and parsed else None


def message_from_row(row) -> "Message":
    """Собрать Message из строки, выбранной по MESSAGE_COLUMNS."""
    return Message(
        timestamp=datetime.fromisoformat(row[0]),
        sender=row[1],
        chat_name=row[2],
        content=row[3],
        is_from_me=row[4],
        chat_jid=row[5],
        id=row[6],
        media_type=row[7],
        reply_to=row[8],
        reply_sender=row[9],
        mentions=mentions_from_row(row[10]),
    )


@dataclass
class Chat:
    jid: str
    name: Optional[str]
    last_message_time: Optional[datetime]
    last_message: Optional[str] = None
    last_sender: Optional[str] = None
    last_is_from_me: Optional[bool] = None

    @property
    def is_group(self) -> bool:
        """Determine if chat is a group based on JID pattern."""
        return self.jid.endswith("@g.us")

@dataclass
class Contact:
    phone_number: str
    name: Optional[str]
    jid: str

@dataclass
class MessageContext:
    message: Message
    before: List[Message]
    after: List[Message]

def get_sender_name(sender_jid: str) -> str:
    try:
        conn = _connect_messages_db()
        cursor = conn.cursor()

        user_part = sender_jid.split('@')[0] if '@' in sender_jid else sender_jid

        # Build the set of JIDs / user parts this sender could appear as.
        # If sender_jid is a LID, include the linked phone JID too, and vice versa.
        jid_variants = {sender_jid}
        try:
            if sender_jid.endswith('@lid') or (user_part.isdigit() and len(user_part) > 13):
                cursor.execute("SELECT pn FROM wa.whatsmeow_lid_map WHERE lid = ?", (user_part,))
                row = cursor.fetchone()
                if row:
                    jid_variants.add(f"{row[0]}@s.whatsapp.net")
            else:
                cursor.execute("SELECT lid FROM wa.whatsmeow_lid_map WHERE pn = ?", (user_part,))
                row = cursor.fetchone()
                if row:
                    jid_variants.add(f"{row[0]}@lid")
        except sqlite3.Error:
            pass

        placeholders = ','.join('?' * len(jid_variants))
        cursor.execute(
            f"SELECT name FROM chats WHERE jid IN ({placeholders}) AND name IS NOT NULL AND name != '' LIMIT 1",
            tuple(jid_variants),
        )
        result = cursor.fetchone()
        if result and result[0]:
            return result[0]

        # Fall back to whatsmeow_contacts (full_name → push_name → business_name)
        try:
            cursor.execute(
                f"""
                SELECT COALESCE(NULLIF(full_name, ''), NULLIF(push_name, ''), NULLIF(business_name, ''))
                FROM wa.whatsmeow_contacts
                WHERE their_jid IN ({placeholders})
                LIMIT 1
                """,
                tuple(jid_variants),
            )
            row = cursor.fetchone()
            if row and row[0]:
                return row[0]
        except sqlite3.Error:
            pass

        return sender_jid

    except sqlite3.Error as e:
        print(f"Database error while getting sender name: {e}", file=sys.stderr)
        return sender_jid
    finally:
        if 'conn' in locals():
            conn.close()

def format_message(message: Message, show_chat_info: bool = True) -> None:
    """Print a single message with consistent formatting."""
    output = ""
    
    if show_chat_info and message.chat_name:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] Chat: {message.chat_name} "
    else:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] "
        
    content_prefix = ""
    if hasattr(message, 'media_type') and message.media_type:
        content_prefix = f"[{message.media_type} - Message ID: {message.id} - Chat JID: {message.chat_jid}] "

    try:
        sender_name = get_sender_name(message.sender) if not message.is_from_me else "Me"

        # Ответ без пометки читается как новая реплика, и в группе непонятно,
        # кому он адресован.
        reply_prefix = ""
        if message.reply_to:
            if message.reply_sender:
                reply_prefix = f"[Reply to {get_sender_name(message.reply_sender)} - Message ID: {message.reply_to}] "
            else:
                reply_prefix = f"[Reply to Message ID: {message.reply_to}] "

        mentions_suffix = ""
        if message.mentions:
            mentioned = ", ".join(get_sender_name(jid) for jid in message.mentions)
            mentions_suffix = f" [Mentions: {mentioned}]"

        output += f"From: {sender_name}: {reply_prefix}{content_prefix}{message.content}{mentions_suffix}\n"
    except Exception as e:
        print(f"Error formatting message: {e}", file=sys.stderr)
    return output

def format_messages_list(messages: List[Message], show_chat_info: bool = True) -> None:
    output = ""
    if not messages:
        output += "No messages to display."
        return output
    
    for message in messages:
        output += format_message(message, show_chat_info)
    return output

def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender_phone_number: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1
) -> List[Message]:
    """Get messages matching the specified criteria with optional context."""
    try:
        conn = _connect_messages_db()
        cursor = conn.cursor()

        # Build base query
        query_parts = [f"SELECT {MESSAGE_COLUMNS} FROM messages"]
        query_parts.append("JOIN chats ON messages.chat_jid = chats.jid")
        where_clauses = []
        params = []

        # Add filters
        if after:
            try:
                after = datetime.fromisoformat(after)
            except ValueError:
                raise ValueError(f"Invalid date format for 'after': {after}. Please use ISO-8601 format.")

            where_clauses.append("messages.timestamp > ?")
            params.append(after)

        if before:
            try:
                before = datetime.fromisoformat(before)
            except ValueError:
                raise ValueError(f"Invalid date format for 'before': {before}. Please use ISO-8601 format.")

            where_clauses.append("messages.timestamp < ?")
            params.append(before)

        if sender_phone_number:
            # Filter against every user-part this phone could store messages under (phone digits + linked LIDs).
            # Also match the chat JID so 1-on-1 lookups work even when the sender column is empty for own messages.
            user_parts = _user_part_candidates_for_phone(conn, sender_phone_number) or [_normalize_phone(sender_phone_number)]
            jid_candidates = _jid_candidates_for_phone(conn, sender_phone_number)
            placeholders_users = ','.join('?' * len(user_parts))
            placeholders_jids = ','.join('?' * len(jid_candidates)) if jid_candidates else None
            if placeholders_jids:
                where_clauses.append(f"(messages.sender IN ({placeholders_users}) OR messages.chat_jid IN ({placeholders_jids}))")
                params.extend(user_parts)
                params.extend(jid_candidates)
            else:
                where_clauses.append(f"messages.sender IN ({placeholders_users})")
                params.extend(user_parts)

        if chat_jid:
            where_clauses.append("messages.chat_jid = ?")
            params.append(chat_jid)
            
        if query:
            where_clauses.append("LOWER(messages.content) LIKE LOWER(?)")
            params.append(f"%{query}%")
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add pagination
        offset = page * limit
        query_parts.append("ORDER BY messages.timestamp DESC")
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        messages = cursor.fetchall()
        
        result = []
        for msg in messages:
            result.append(message_from_row(msg))
            
        if include_context and result:
            # Add context for each message
            messages_with_context = []
            for msg in result:
                context = get_message_context(msg.id, context_before, context_after)
                messages_with_context.extend(context.before)
                messages_with_context.append(context.message)
                messages_with_context.extend(context.after)
            
            return format_messages_list(messages_with_context, show_chat_info=True)
            
        # Format and display messages without context
        return format_messages_list(result, show_chat_info=True)    
        
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> MessageContext:
    """Get context around a specific message."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Get the target message first
        cursor.execute(f"""
            SELECT {MESSAGE_COLUMNS}
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.id = ?
        """, (message_id,))
        msg_data = cursor.fetchone()
        
        if not msg_data:
            raise ValueError(f"Message with ID {message_id} not found")
            
        target_message = message_from_row(msg_data)
        
        # Get messages before
        cursor.execute(f"""
            SELECT {MESSAGE_COLUMNS}
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp < ?
            ORDER BY messages.timestamp DESC
            LIMIT ?
        """, (msg_data[5], msg_data[0], before))
        
        before_messages = [message_from_row(msg) for msg in cursor.fetchall()]
        
        # Get messages after
        cursor.execute(f"""
            SELECT {MESSAGE_COLUMNS}
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp > ?
            ORDER BY messages.timestamp ASC
            LIMIT ?
        """, (msg_data[5], msg_data[0], after))
        
        after_messages = [message_from_row(msg) for msg in cursor.fetchall()]
        
        return MessageContext(
            message=target_message,
            before=before_messages,
            after=after_messages
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        raise
    finally:
        if 'conn' in locals():
            conn.close()


def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> List[Chat]:
    """Get chats matching the specified criteria."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Build base query
        query_parts = ["""
            SELECT 
                chats.jid,
                chats.name,
                chats.last_message_time,
                messages.content as last_message,
                messages.sender as last_sender,
                messages.is_from_me as last_is_from_me
            FROM chats
        """]
        
        if include_last_message:
            query_parts.append("""
                LEFT JOIN messages ON chats.jid = messages.chat_jid 
                AND chats.last_message_time = messages.timestamp
            """)
            
        where_clauses = []
        params = []
        
        if query:
            where_clauses.append("(LOWER(chats.name) LIKE LOWER(?) OR chats.jid LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add sorting
        order_by = "chats.last_message_time DESC" if sort_by == "last_active" else "chats.name"
        query_parts.append(f"ORDER BY {order_by}")
        
        # Add pagination
        offset = (page ) * limit
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        chats = cursor.fetchall()
        
        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=chat_data[1],
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5]
            )
            result.append(chat)
            
        return result
        
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def search_contacts(query: str) -> List[Contact]:
    """Search contacts by name or phone number — looks across chat names, the WhatsApp contact book, and the LID map."""
    try:
        conn = _connect_messages_db()
        cursor = conn.cursor()

        search_pattern = '%' + query + '%'
        digits = _normalize_phone(query)

        # Pull JIDs from three sources and union them:
        # 1. chats table (existing behaviour)
        # 2. wa.whatsmeow_contacts (covers contacts WhatsApp synced but we have no chat row for, and matches by full_name/push_name)
        # 3. wa.whatsmeow_lid_map (resolves a phone number to its LID JID and vice versa)
        jid_to_name: dict[str, Optional[str]] = {}

        cursor.execute(
            """
            SELECT DISTINCT jid, name
            FROM chats
            WHERE (LOWER(name) LIKE LOWER(?) OR LOWER(jid) LIKE LOWER(?))
              AND jid NOT LIKE '%@g.us'
            """,
            (search_pattern, search_pattern),
        )
        for jid, name in cursor.fetchall():
            jid_to_name[jid] = name

        try:
            cursor.execute(
                """
                SELECT their_jid,
                       COALESCE(NULLIF(full_name, ''), NULLIF(push_name, ''), NULLIF(business_name, '')) AS display_name
                FROM wa.whatsmeow_contacts
                WHERE their_jid NOT LIKE '%@g.us'
                  AND (LOWER(COALESCE(full_name, '')) LIKE LOWER(?)
                       OR LOWER(COALESCE(push_name, '')) LIKE LOWER(?)
                       OR LOWER(COALESCE(business_name, '')) LIKE LOWER(?)
                       OR LOWER(their_jid) LIKE LOWER(?))
                """,
                (search_pattern, search_pattern, search_pattern, search_pattern),
            )
            for jid, name in cursor.fetchall():
                if jid not in jid_to_name or not jid_to_name[jid]:
                    jid_to_name[jid] = name
        except sqlite3.Error:
            pass

        if digits:
            try:
                # Phone → LID
                cursor.execute("SELECT lid, pn FROM wa.whatsmeow_lid_map WHERE pn = ? OR lid = ?", (digits, digits))
                for lid, pn in cursor.fetchall():
                    for jid in (f"{pn}@s.whatsapp.net", f"{lid}@lid"):
                        if jid not in jid_to_name:
                            jid_to_name[jid] = None
            except sqlite3.Error:
                pass

        result: List[Contact] = []
        for jid, name in jid_to_name.items():
            user_part = jid.split('@')[0]
            # For LID JIDs, expose the linked phone in phone_number when we can — that's what callers usually want.
            phone_for_display = user_part
            if jid.endswith('@lid'):
                try:
                    cursor.execute("SELECT pn FROM wa.whatsmeow_lid_map WHERE lid = ?", (user_part,))
                    row = cursor.fetchone()
                    if row:
                        phone_for_display = row[0]
                except sqlite3.Error:
                    pass

            if not name:
                # Fallback to whatever name we have for this JID in chats/contacts
                name = get_sender_name(jid)
                if name == jid:
                    name = None

            result.append(Contact(phone_number=phone_for_display, name=name, jid=jid))

        result.sort(key=lambda c: ((c.name or '').lower(), c.jid))
        return result[:50]

    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Chat]:
    """Get all chats involving the contact.
    
    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    try:
        conn = _connect_messages_db()
        cursor = conn.cursor()
        jid_aliases = _jid_aliases_for_identifier(conn, jid)
        sender_aliases = _user_part_aliases_for_identifier(conn, jid)
        if not jid_aliases and not sender_aliases:
            return []

        jid_placeholders = ','.join('?' * len(jid_aliases)) or "NULL"
        sender_placeholders = ','.join('?' * len(sender_aliases)) or "NULL"
        
        cursor.execute(f"""
            SELECT DISTINCT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            JOIN messages m ON c.jid = m.chat_jid
            WHERE m.sender IN ({sender_placeholders}) OR c.jid IN ({jid_placeholders})
            ORDER BY c.last_message_time DESC
            LIMIT ? OFFSET ?
        """, tuple(sender_aliases + jid_aliases + [limit, page * limit]))
        
        chats = cursor.fetchall()
        
        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=chat_data[1],
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5]
            )
            result.append(chat)
            
        return result
        
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_last_interaction(jid: str) -> str:
    """Get most recent message involving the contact."""
    try:
        conn = _connect_messages_db()
        cursor = conn.cursor()
        jid_aliases = _jid_aliases_for_identifier(conn, jid)
        sender_aliases = _user_part_aliases_for_identifier(conn, jid)
        if not jid_aliases and not sender_aliases:
            return None

        jid_placeholders = ','.join('?' * len(jid_aliases)) or "NULL"
        sender_placeholders = ','.join('?' * len(sender_aliases)) or "NULL"
        
        cursor.execute(f"""
            SELECT 
                m.timestamp,
                m.sender,
                c.name,
                m.content,
                m.is_from_me,
                c.jid,
                m.id,
                m.media_type
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.sender IN ({sender_placeholders}) OR c.jid IN ({jid_placeholders})
            ORDER BY m.timestamp DESC
            LIMIT 1
        """, tuple(sender_aliases + jid_aliases))
        
        msg_data = cursor.fetchone()
        
        if not msg_data:
            return None
            
        message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[7]
        )
        
        return format_message(message)
        
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Chat]:
    """Get chat metadata by JID."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        query = """
            SELECT 
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
        """
        
        if include_last_message:
            query += """
                LEFT JOIN messages m ON c.jid = m.chat_jid 
                AND c.last_message_time = m.timestamp
            """
            
        query += " WHERE c.jid = ?"
        
        cursor.execute(query, (chat_jid,))
        chat_data = cursor.fetchone()
        
        if not chat_data:
            return None
            
        return Chat(
            jid=chat_data[0],
            name=chat_data[1],
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Chat]:
    """Get chat metadata by sender phone number — checks both phone JID and any linked LID JID."""
    try:
        conn = _connect_messages_db()
        cursor = conn.cursor()

        candidates = _jid_candidates_for_phone(conn, sender_phone_number)
        if not candidates:
            return None

        # Prefer the most recently active matching chat.
        placeholders = ','.join('?' * len(candidates))
        cursor.execute(
            f"""
            SELECT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            LEFT JOIN messages m ON c.jid = m.chat_jid
                AND c.last_message_time = m.timestamp
            WHERE c.jid IN ({placeholders}) AND c.jid NOT LIKE '%@g.us'
            ORDER BY c.last_message_time DESC
            LIMIT 1
            """,
            tuple(candidates),
        )

        chat_data = cursor.fetchone()

        if not chat_data:
            return None

        return Chat(
            jid=chat_data[0],
            name=chat_data[1],
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )

    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return None
    finally:
        if 'conn' in locals():
            conn.close()

def send_message(recipient: str, message: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "message": message,
        }
        
        response = requests.post(url, json=payload, timeout=BRIDGE_TIMEOUT)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_file(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload, timeout=BRIDGE_MEDIA_TIMEOUT)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_audio_message(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"

        if not media_path.endswith(".ogg"):
            try:
                media_path = audio.convert_to_opus_ogg_temp(media_path)
            except Exception as e:
                return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {str(e)}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload, timeout=BRIDGE_MEDIA_TIMEOUT)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def download_media(message_id: str, chat_jid: str) -> Optional[str]:
    """Download media from a message and return the local file path.
    
    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    
    Returns:
        The local file path if download was successful, None otherwise
    """
    try:
        url = f"{WHATSAPP_API_BASE_URL}/download"
        payload = {
            "message_id": message_id,
            "chat_jid": chat_jid
        }
        
        response = requests.post(url, json=payload, timeout=BRIDGE_MEDIA_TIMEOUT)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("success", False):
                path = result.get("path")
                print(f"Media downloaded successfully: {path}", file=sys.stderr)
                return path
            else:
                print(f"Download failed: {result.get('message', 'Unknown error')}", file=sys.stderr)
                return None
        else:
            print(f"Error: HTTP {response.status_code} - {response.text}", file=sys.stderr)
            return None
            
    except requests.RequestException as e:
        print(f"Request error: {str(e)}", file=sys.stderr)
        return None
    except json.JSONDecodeError:
        print(f"Error parsing response: {response.text}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Unexpected error: {str(e)}", file=sys.stderr)
        return None
