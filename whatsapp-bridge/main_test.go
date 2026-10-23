package main

import (
	"testing"

	waProto "go.mau.fi/whatsmeow/binary/proto"
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
