package main

import (
	"encoding/json"
	wxproto "github.com/yincongcyincong/weixin-macos/onebot/proto"
	"testing"
)

func TestPreserveOriginalParticipantsAcrossHTTPNormalization(t *testing.T) {
	for _, c := range []struct{ from, to string }{
		{"wxid_self_fixture", "12345@chatroom"},
		{"wxid_self_fixture", "wxid_peer_fixture"},
		{"12345@chatroom", "wxid_self_fixture"},
	} {
		data := &wxproto.WxRecvMsgData{Sender: &wxproto.WxString{Value: c.from}, Receiver: &wxproto.WxString{Value: c.to}, Content: &wxproto.WxString{Value: "仅离线测试"}, ContentType: 1, MsgId: 12345}
		raw, err := buildWechatMessageJSON(data)
		if err != nil {
			t.Fatal(err)
		}
		normalized, err := HandleMsg(raw)
		if err != nil {
			t.Fatal(err)
		}
		var result map[string]any
		if err = json.Unmarshal(normalized, &result); err != nil {
			t.Fatal(err)
		}
		if result["from_user_id"] != c.from || result["to_user_id"] != c.to {
			t.Fatalf("participant metadata lost: %v", result)
		}
	}
}
