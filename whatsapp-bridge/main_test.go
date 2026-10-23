package main

import (
	"database/sql"
	"path/filepath"
	"strings"
	"testing"
	"time"

	waProto "go.mau.fi/whatsmeow/binary/proto"
	"go.mau.fi/whatsmeow/proto/waCommon"
	// Часть типов в старый псевдоним binary/proto не попала.
	"go.mau.fi/whatsmeow/proto/waE2E"
	"google.golang.org/protobuf/proto"
)

func TestReplyAndMentionsFromExtendedText(t *testing.T) {
	msg := &waProto.Message{
		ExtendedTextMessage: &waProto.ExtendedTextMessage{
			Text: proto.String("отвечаю"),
			ContextInfo: &waProto.ContextInfo{
				StanzaID:     proto.String("ABC123"),
				Participant:  proto.String("40733873140@s.whatsapp.net"),
				MentionedJID: []string{"111@s.whatsapp.net", "222@s.whatsapp.net"},
			},
		},
	}

	replyTo, replySender, mentions := replyAndMentions(msg)
	if replyTo != "ABC123" {
		t.Errorf("replyTo = %q, want ABC123", replyTo)
	}
	if replySender != "40733873140@s.whatsapp.net" {
		t.Errorf("replySender = %q", replySender)
	}
	if len(mentions) != 2 || mentions[0] != "111@s.whatsapp.net" {
		t.Errorf("mentions = %v", mentions)
	}
}

// Картинка, видео и документ носят своё поле contextInfo, и ответ картинкой
// должен читаться так же, как ответ текстом.
func TestReplyAndMentionsFromImage(t *testing.T) {
	msg := &waProto.Message{
		ImageMessage: &waProto.ImageMessage{
			Caption: proto.String("вот оно"),
			ContextInfo: &waProto.ContextInfo{
				StanzaID: proto.String("IMG777"),
			},
		},
	}

	replyTo, replySender, mentions := replyAndMentions(msg)
	if replyTo != "IMG777" {
		t.Errorf("replyTo = %q, want IMG777", replyTo)
	}
	if replySender != "" || mentions != nil {
		t.Errorf("replySender = %q, mentions = %v, want empty", replySender, mentions)
	}
}

// Обычная реплика без цитаты: автор цитаты не придумывается.
func TestReplyAndMentionsPlainMessage(t *testing.T) {
	msg := &waProto.Message{Conversation: proto.String("просто текст")}

	replyTo, replySender, mentions := replyAndMentions(msg)
	if replyTo != "" || replySender != "" || mentions != nil {
		t.Errorf("got %q / %q / %v, want all empty", replyTo, replySender, mentions)
	}
}

// Упоминания без цитаты: участник из ContextInfo не выдаётся за автора цитаты.
func TestMentionsWithoutReply(t *testing.T) {
	msg := &waProto.Message{
		ExtendedTextMessage: &waProto.ExtendedTextMessage{
			Text: proto.String("@кто-то смотри"),
			ContextInfo: &waProto.ContextInfo{
				Participant:  proto.String("40733873140@s.whatsapp.net"),
				MentionedJID: []string{"333@s.whatsapp.net"},
			},
		},
	}

	replyTo, replySender, mentions := replyAndMentions(msg)
	if replyTo != "" {
		t.Errorf("replyTo = %q, want empty", replyTo)
	}
	if replySender != "" {
		t.Errorf("replySender = %q, want empty without a quote", replySender)
	}
	if len(mentions) != 1 {
		t.Errorf("mentions = %v", mentions)
	}
}

// Имена медиа: два файла, пришедшие в одну секунду, раньше затирали друг друга.
func TestMediaFilePrefix(t *testing.T) {
	cases := map[string]string{
		"3B3338623DCD3D1621E5": "3B3338623DC",
		"":                     "msg",
		"!!!":                  "msg",
		"abc-def":              "abcdef",
	}
	for id, want := range cases {
		got := mediaFilePrefix(id)
		if id == "3B3338623DCD3D1621E5" {
			want = "3B3338623DCD"
		}
		if got != want {
			t.Errorf("mediaFilePrefix(%q) = %q, want %q", id, got, want)
		}
	}
	if a, b := mediaFilePrefix("AAA111"), mediaFilePrefix("BBB222"); a == b {
		t.Errorf("different ids share a prefix: %q", a)
	}
}

// Подпись к файлу: до этого её не читал никто, и все 1906 медиасообщений
// в базе лежали с пустым content.
func TestExtractTextContentCaptions(t *testing.T) {
	cases := map[string]*waProto.Message{
		"фото с подписью":  {ImageMessage: &waProto.ImageMessage{Caption: proto.String("фото с подписью")}},
		"видео с подписью": {VideoMessage: &waProto.VideoMessage{Caption: proto.String("видео с подписью")}},
		"счёт за октябрь": {DocumentMessage: &waProto.DocumentMessage{
			FileName: proto.String("invoice.pdf"),
			Caption:  proto.String("счёт за октябрь"),
		}},
	}
	for want, msg := range cases {
		if got := extractTextContent(msg); got != want {
			t.Errorf("extractTextContent = %q, want %q", got, want)
		}
	}
}

// Нетекстовое приходило в базу тишиной: строки не было вовсе, а время
// последнего сообщения в чате обновлялось.
func TestExtractTextContentNonText(t *testing.T) {
	poll := &waProto.PollCreationMessage{
		Name: proto.String("Когда едем"),
		Options: []*waProto.PollCreationMessage_Option{
			{OptionName: proto.String("в пятницу")},
			{OptionName: proto.String("в субботу")},
		},
	}

	cases := []struct {
		name string
		msg  *waProto.Message
		want string
	}{
		{"место", &waProto.Message{LocationMessage: &waProto.LocationMessage{
			Name:             proto.String("Дом"),
			Address:          proto.String("Strada Lunga 3"),
			DegreesLatitude:  proto.Float64(44.4268),
			DegreesLongitude: proto.Float64(26.1025),
		}}, "[location: Дом Strada Lunga 3 (44.4268, 26.1025)]"},
		{"место без подписи", &waProto.Message{LocationMessage: &waProto.LocationMessage{
			DegreesLatitude:  proto.Float64(44.5),
			DegreesLongitude: proto.Float64(26.0),
		}}, "[location: (44.5, 26)]"},
		{"живое место", &waProto.Message{LiveLocationMessage: &waProto.LiveLocationMessage{
			Caption:          proto.String("иду"),
			DegreesLatitude:  proto.Float64(44.1),
			DegreesLongitude: proto.Float64(26.2),
		}}, "[live location: иду (44.1, 26.2)]"},
		{"визитка", &waProto.Message{ContactMessage: &waProto.ContactMessage{
			DisplayName: proto.String("Валентин"),
		}}, "[contact: Валентин]"},
		{"визитка без имени", &waProto.Message{ContactMessage: &waProto.ContactMessage{}}, "[contact]"},
		{"несколько визиток", &waProto.Message{ContactsArrayMessage: &waProto.ContactsArrayMessage{
			Contacts: []*waProto.ContactMessage{
				{DisplayName: proto.String("Вика")},
				{DisplayName: proto.String("Саша")},
			},
		}}, "[contacts: Вика, Саша]"},
		{"опрос", &waProto.Message{PollCreationMessage: poll},
			"[poll: Когда едем options: в пятницу, в субботу]"},
		{"опрос v3", &waProto.Message{PollCreationMessageV3: poll},
			"[poll: Когда едем options: в пятницу, в субботу]"},
		{"опрос v6", &waProto.Message{PollCreationMessageV6: poll},
			"[poll: Когда едем options: в пятницу, в субботу]"},
		{"опрос v4 в обёртке", &waProto.Message{PollCreationMessageV4: &waProto.FutureProofMessage{
			Message: &waProto.Message{PollCreationMessageV3: poll},
		}}, "[poll: Когда едем options: в пятницу, в субботу]"},
		{"голос в опросе", &waProto.Message{PollUpdateMessage: &waProto.PollUpdateMessage{}}, "[poll vote]"},
		{"новый вариант", &waProto.Message{PollAddOptionMessage: &waE2E.PollAddOptionMessage{
			AddOption: &waProto.PollCreationMessage_Option{OptionName: proto.String("в воскресенье")},
		}}, "[poll option added: в воскресенье]"},
	}

	for _, c := range cases {
		if got := extractTextContent(c.msg); got != c.want {
			t.Errorf("%s: extractTextContent = %q, want %q", c.name, got, c.want)
		}
	}
}

// Обёртки: одноразовое фото, исчезающее сообщение и документ с подписью
// несут настоящее сообщение внутри. Документ с подписью так и приходит,
// и раньше терялся целиком - ни файла, ни сказанного рядом.
func TestUnwrappedMessages(t *testing.T) {
	viewOnce := &waProto.Message{ViewOnceMessageV2: &waProto.FutureProofMessage{
		Message: &waProto.Message{ImageMessage: &waProto.ImageMessage{
			Caption: proto.String("только раз"),
			URL:     proto.String("https://mmg.whatsapp.net/v/t62"),
		}},
	}}
	if got := extractTextContent(viewOnce); got != "только раз" {
		t.Errorf("view once caption = %q", got)
	}
	if mediaType, _, _, _, _, _, _, _ := extractMediaInfo(viewOnce); mediaType != "image" {
		t.Errorf("view once media type = %q, want image", mediaType)
	}

	ephemeral := &waProto.Message{EphemeralMessage: &waProto.FutureProofMessage{
		Message: &waProto.Message{Conversation: proto.String("исчезнет")},
	}}
	if got := extractTextContent(ephemeral); got != "исчезнет" {
		t.Errorf("ephemeral text = %q", got)
	}

	withCaption := &waProto.Message{DocumentWithCaptionMessage: &waProto.FutureProofMessage{
		Message: &waProto.Message{DocumentMessage: &waProto.DocumentMessage{
			FileName: proto.String("stat-de-plata.pdf"),
			Caption:  proto.String("ведомость за сентябрь"),
			URL:      proto.String("https://mmg.whatsapp.net/v/t62"),
		}},
	}}
	if got := extractTextContent(withCaption); got != "ведомость за сентябрь" {
		t.Errorf("document caption = %q", got)
	}
	mediaType, filename, _, _, _, _, _, _ := extractMediaInfo(withCaption)
	if mediaType != "document" || filename != "stat-de-plata.pdf" {
		t.Errorf("document in a caption wrapper = %q / %q", mediaType, filename)
	}

	// Цитата у завёрнутого сообщения тоже должна находиться.
	quoted := &waProto.Message{ViewOnceMessageV2: &waProto.FutureProofMessage{
		Message: &waProto.Message{ImageMessage: &waProto.ImageMessage{
			ContextInfo: &waProto.ContextInfo{StanzaID: proto.String("WRAP1")},
		}},
	}}
	if replyTo, _, _ := replyAndMentions(quoted); replyTo != "WRAP1" {
		t.Errorf("replyTo in a wrapper = %q, want WRAP1", replyTo)
	}
}

// Стикер - такой же файл на CDN, только скачивается ключами картинки.
func TestExtractMediaInfoSticker(t *testing.T) {
	msg := &waProto.Message{StickerMessage: &waProto.StickerMessage{
		URL:        proto.String("https://mmg.whatsapp.net/v/t62"),
		DirectPath: proto.String("/v/t62?oh=1&oe=2&ccb=9"),
		MediaKey:   []byte("key"),
		FileLength: proto.Uint64(4096),
	}}
	mediaType, filename, url, directPath, mediaKey, _, _, fileLength := extractMediaInfo(msg)
	if mediaType != "sticker" {
		t.Fatalf("media type = %q, want sticker", mediaType)
	}
	if !strings.HasSuffix(filename, ".webp") {
		t.Errorf("filename = %q, want a .webp name", filename)
	}
	if url == "" || directPath != "/v/t62?oh=1&oe=2&ccb=9" || len(mediaKey) == 0 || fileLength != 4096 {
		t.Errorf("sticker lost its download info: %q / %q / %d", url, directPath, fileLength)
	}
	// Текста у стикера нет, и выдумывать его незачем: он лежит в media_type.
	if got := extractTextContent(msg); got != "" {
		t.Errorf("sticker text = %q, want empty", got)
	}
}

// Правка открытым протокольным сообщением: цель - ключ внутри, а не номер
// самого события (тот указывает на правку).
func TestEditFromProtocol(t *testing.T) {
	edit := &waProto.Message{ProtocolMessage: &waProto.ProtocolMessage{
		Key:  &waCommon.MessageKey{ID: proto.String("TARGET1")},
		Type: waProto.ProtocolMessage_MESSAGE_EDIT.Enum(),
		EditedMessage: &waProto.Message{
			Conversation: proto.String("так правильнее"),
		},
	}}

	target, edited, ok := editFromProtocol(edit)
	if !ok || target != "TARGET1" {
		t.Fatalf("editFromProtocol = %q, %v", target, ok)
	}
	if got := extractTextContent(edited); got != "так правильнее" {
		t.Errorf("edited text = %q", got)
	}
	if got := editedContent(edit); got != "так правильнее" {
		t.Errorf("editedContent of a protocol envelope = %q", got)
	}
	if _, ok := revokeFromProtocol(edit); ok {
		t.Error("an edit was read as a deletion")
	}
}

// «Удалить у всех».
func TestRevokeFromProtocol(t *testing.T) {
	revoke := &waProto.Message{ProtocolMessage: &waProto.ProtocolMessage{
		Key:  &waCommon.MessageKey{ID: proto.String("GONE1")},
		Type: waProto.ProtocolMessage_REVOKE.Enum(),
	}}

	target, ok := revokeFromProtocol(revoke)
	if !ok || target != "GONE1" {
		t.Fatalf("revokeFromProtocol = %q, %v", target, ok)
	}
	if _, _, ok := editFromProtocol(revoke); ok {
		t.Error("a deletion was read as an edit")
	}
}

// Служебное протокольное сообщение приходит без типа вовсе: раздача ключей
// истории, сверка состояния. Нулевое значение перечисления - это REVOKE,
// и геттер отдаёт его же для пустого поля, так что без проверки на nil такое
// сообщение помечало бы удалённым первое попавшееся.
func TestProtocolWithoutTypeIsNeitherEditNorRevoke(t *testing.T) {
	service := &waProto.Message{ProtocolMessage: &waProto.ProtocolMessage{
		HistorySyncNotification: &waProto.HistorySyncNotification{
			FileLength: proto.Uint64(1024),
		},
	}}
	if target, ok := revokeFromProtocol(service); ok {
		t.Errorf("service protocol message read as a deletion of %q", target)
	}
	if _, _, ok := editFromProtocol(service); ok {
		t.Error("service protocol message read as an edit")
	}

	// Тип назван, а ключа нет: править или удалять нечего.
	keyless := &waProto.Message{ProtocolMessage: &waProto.ProtocolMessage{
		Type: waProto.ProtocolMessage_REVOKE.Enum(),
	}}
	if _, ok := revokeFromProtocol(keyless); ok {
		t.Error("a deletion without a target was accepted")
	}
}

// Правка у старых клиентов завёрнута в editedMessage, а внутри тот же
// протокольный конверт.
func TestEditInsideEditedMessageWrapper(t *testing.T) {
	wrapped := &waProto.Message{EditedMessage: &waProto.FutureProofMessage{
		Message: &waProto.Message{ProtocolMessage: &waProto.ProtocolMessage{
			Key:           &waCommon.MessageKey{ID: proto.String("OLD1")},
			Type:          waProto.ProtocolMessage_MESSAGE_EDIT.Enum(),
			EditedMessage: &waProto.Message{Conversation: proto.String("поправил")},
		}},
	}}

	target, edited, ok := editFromProtocol(wrapped)
	if !ok || target != "OLD1" {
		t.Fatalf("editFromProtocol = %q, %v", target, ok)
	}
	if got := extractTextContent(edited); got != "поправил" {
		t.Errorf("edited text = %q", got)
	}
}

// Реакции и правки на своей временной базе: та же схема, что у живого моста.
func TestReactionsEditsAndDeletionsInStore(t *testing.T) {
	store, err := NewMessageStoreAt(filepath.Join(t.TempDir(), "messages.db"))
	if err != nil {
		t.Fatalf("NewMessageStoreAt: %v", err)
	}
	defer store.Close()

	chat := "123456789@s.whatsapp.net"
	now := time.Now().Truncate(time.Second)
	if err := store.StoreChat(chat, "Вика", now); err != nil {
		t.Fatalf("StoreChat: %v", err)
	}
	if err := store.StoreMessage(MessageRecord{
		ID: "MSG1", ChatJID: chat, Sender: "123456789", Content: "первая версия", Timestamp: now,
	}); err != nil {
		t.Fatalf("StoreMessage: %v", err)
	}

	// Реакция заменяется, а не копится: в WhatsApp она одна на человека.
	if err := store.StoreReaction("MSG1", chat, "123456789", "👍", now); err != nil {
		t.Fatalf("StoreReaction: %v", err)
	}
	if err := store.StoreReaction("MSG1", chat, "123456789", "❤️", now.Add(time.Minute)); err != nil {
		t.Fatalf("StoreReaction again: %v", err)
	}
	reactions, err := store.Reactions("MSG1", chat)
	if err != nil {
		t.Fatalf("Reactions: %v", err)
	}
	if len(reactions) != 1 || reactions["123456789"] != "❤️" {
		t.Errorf("reactions = %v, want one ❤️", reactions)
	}

	// Реакция на сообщение, которого у нас нет, всё равно сохраняется:
	// она приходит и на то, что старше базы.
	if err := store.StoreReaction("UNKNOWN", chat, "123456789", "😂", now); err != nil {
		t.Errorf("reaction on an unknown message: %v", err)
	}

	if err := store.RemoveReaction("MSG1", chat, "123456789"); err != nil {
		t.Fatalf("RemoveReaction: %v", err)
	}
	if reactions, _ := store.Reactions("MSG1", chat); len(reactions) != 0 {
		t.Errorf("reactions after removal = %v", reactions)
	}

	// Правка меняет текст и отмечает время правки.
	found, err := store.ApplyEdit("MSG1", chat, "вторая версия", now.Add(2*time.Minute))
	if err != nil || !found {
		t.Fatalf("ApplyEdit = %v, %v", found, err)
	}
	var content string
	var editedAt sql.NullTime
	if err := store.db.QueryRow(
		"SELECT content, edited_at FROM messages WHERE id = ? AND chat_jid = ?", "MSG1", chat,
	).Scan(&content, &editedAt); err != nil {
		t.Fatalf("read back: %v", err)
	}
	if content != "вторая версия" || !editedAt.Valid {
		t.Errorf("after the edit: content %q, edited_at valid %v", content, editedAt.Valid)
	}

	// Правка сообщения, которого нет: это видно по ответу, и в лог уходит
	// строка, а не тишина.
	if found, err := store.ApplyEdit("MISSING", chat, "текст", now); err != nil || found {
		t.Errorf("ApplyEdit of a missing message = %v, %v", found, err)
	}

	// Удаление у всех: текст остаётся, помечается время.
	if found, err := store.MarkRevoked("MSG1", chat, now.Add(3*time.Minute)); err != nil || !found {
		t.Fatalf("MarkRevoked = %v, %v", found, err)
	}
	var revokedAt sql.NullTime
	if err := store.db.QueryRow(
		"SELECT content, revoked_at FROM messages WHERE id = ? AND chat_jid = ?", "MSG1", chat,
	).Scan(&content, &revokedAt); err != nil {
		t.Fatalf("read back: %v", err)
	}
	if content != "вторая версия" || !revokedAt.Valid {
		t.Errorf("after the deletion: content %q, revoked_at valid %v", content, revokedAt.Valid)
	}
}

// С какого сообщения просить историю: телефон отдаёт то, что лежит перед
// названным, и назвать надо самое старое из сохранённых.
func TestOldestMessageOfChat(t *testing.T) {
	store, err := NewMessageStoreAt(filepath.Join(t.TempDir(), "messages.db"))
	if err != nil {
		t.Fatalf("NewMessageStoreAt: %v", err)
	}
	defer store.Close()

	chat := "120363027275625969@g.us"
	other := "123456789@s.whatsapp.net"
	start := time.Date(2026, 3, 1, 12, 0, 0, 0, time.UTC)
	for _, c := range []string{chat, other} {
		if err := store.StoreChat(c, "чат", start); err != nil {
			t.Fatalf("StoreChat: %v", err)
		}
	}

	// Порядок записи нарочно не по времени: край истории ищется запросом,
	// а не тем, что легло первым.
	for _, m := range []struct {
		id       string
		chat     string
		offset   time.Duration
		isFromMe bool
	}{
		{"NEW", chat, 48 * time.Hour, false},
		{"OLD", chat, 0, true},
		{"MID", chat, 24 * time.Hour, false},
		{"OTHER", other, -72 * time.Hour, false},
	} {
		if err := store.StoreMessage(MessageRecord{
			ID: m.id, ChatJID: m.chat, Sender: "123456789", Content: m.id,
			Timestamp: start.Add(m.offset), IsFromMe: m.isFromMe,
		}); err != nil {
			t.Fatalf("StoreMessage %s: %v", m.id, err)
		}
	}

	oldest, err := store.OldestMessage(chat)
	if err != nil {
		t.Fatalf("OldestMessage: %v", err)
	}
	if oldest.ID != "OLD" {
		t.Errorf("oldest id = %q, want OLD", oldest.ID)
	}
	if !oldest.Timestamp.Equal(start) {
		t.Errorf("oldest timestamp = %s, want %s", oldest.Timestamp, start)
	}
	if !oldest.IsFromMe {
		t.Error("isFromMe lost: WhatsApp asks for it with the request")
	}
	if oldest.Chat.String() != chat {
		t.Errorf("chat = %q, want %q", oldest.Chat.String(), chat)
	}

	// Чат, которого нет: просить историю не от чего, и это видно по ошибке.
	if _, err := store.OldestMessage("999@s.whatsapp.net"); err != sql.ErrNoRows {
		t.Errorf("OldestMessage of an unknown chat = %v, want sql.ErrNoRows", err)
	}
}
