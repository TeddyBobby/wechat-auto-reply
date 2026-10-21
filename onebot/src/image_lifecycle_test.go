package main

import "testing"

func TestRecoveredImageReleasesEncryptedMemory(t *testing.T) {
	req := &DownloadRequest{CDNURL: "isolated-cdn-key", Media: make([]byte, 1024)}
	rememberImagePath(req, "/isolated/image.jpg")
	if req.FilePath != "/isolated/image.jpg" {
		t.Fatal("successful path not retained")
	}
	if len(req.Media) != 0 {
		t.Fatal("encrypted image buffer retained after successful materialization")
	}
}
