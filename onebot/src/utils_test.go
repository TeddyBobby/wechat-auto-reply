package main

import (
	"encoding/base64"
	"os"
	"path/filepath"
	"testing"
)

func TestFileToBase64(t *testing.T) {
	path := filepath.Join(t.TempDir(), "synthetic.txt")
	data := []byte("synthetic offline fixture")
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	got, err := FileToBase64(path)
	if err != nil {
		t.Fatal(err)
	}
	if got != base64.StdEncoding.EncodeToString(data) {
		t.Fatalf("unexpected base64 output: %q", got)
	}
	if _, err := FileToBase64(filepath.Join(t.TempDir(), "missing.txt")); err == nil {
		t.Fatal("missing file must return an error")
	}
}
