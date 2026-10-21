package main

// 离线测试：微信图片被动缓存恢复。
// 全部离线：只用合成 AES 图片数据 + 可注入 clock/sleeper/缓存查找，不连网、不触发 native 下载。

import (
	"bytes"
	"crypto/aes"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	"image/color"
	"image/jpeg"
	"image/png"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	wxproto "github.com/yincongcyincong/weixin-macos/onebot/proto"
)

const (
	testImageKeyHex    = "0123456789abcdef0123456789abcdef" // 16 bytes
	testImageKeyHexAlt = "11111111111111111111111111111111"
)

// 人工生成的微型 WebP fixture（PIL lossless，VP8L，38 字节），非聊天媒体。
const testWebPBase64 = "UklGRh4AAABXRUJQVlA4TBEAAAAvB8ABAAdQjyLXo/+BiOh/AAA="

func webpFixture(t *testing.T) []byte {
	t.Helper()
	data, err := base64.StdEncoding.DecodeString(testWebPBase64)
	if err != nil {
		t.Fatalf("webp fixture decode: %v", err)
	}
	if _, err := sniffSupportedImage(data); err != nil {
		t.Fatalf("webp fixture 不被识别: %v", err)
	}
	return data
}

func pngFixture(t *testing.T) []byte {
	t.Helper()
	img := image.NewRGBA(image.Rect(0, 0, 4, 4))
	img.Set(0, 0, color.RGBA{R: 10, G: 200, B: 30, A: 255})
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		t.Fatalf("png encode: %v", err)
	}
	return buf.Bytes()
}

func jpegFixture(t *testing.T) []byte {
	t.Helper()
	img := image.NewRGBA(image.Rect(0, 0, 4, 4))
	img.Set(1, 1, color.RGBA{R: 200, G: 10, B: 30, A: 255})
	var buf bytes.Buffer
	if err := jpeg.Encode(&buf, img, nil); err != nil {
		t.Fatalf("jpeg encode: %v", err)
	}
	return buf.Bytes()
}

// encryptImagePayload 模拟微信 CDN 图片：明文零补齐到 AES 块后 AES-ECB 加密。
func encryptImagePayload(t *testing.T, keyHex string, plain []byte) []byte {
	t.Helper()
	key, err := hex.DecodeString(keyHex)
	if err != nil {
		t.Fatalf("key decode: %v", err)
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		t.Fatalf("cipher: %v", err)
	}
	padded := append([]byte(nil), plain...)
	for len(padded)%aes.BlockSize != 0 {
		padded = append(padded, 0)
	}
	out := make([]byte, len(padded))
	for i := 0; i < len(padded); i += aes.BlockSize {
		block.Encrypt(out[i:i+aes.BlockSize], padded[i:i+aes.BlockSize])
	}
	return out
}

// fakeClock 让"等待/超时"在测试里可注入且瞬时。
type fakeClock struct {
	mu     sync.Mutex
	now    time.Time
	sleeps int
	total  time.Duration
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) Sleep(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
	c.sleeps++
	c.total += d
}

func (c *fakeClock) sleepCount() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.sleeps
}

func entry(url string, media []byte, appendedAt time.Time) *DownloadRequest {
	return &DownloadRequest{CDNURL: url, Media: media, LastAppendTime: appendedAt.UnixMilli()}
}

func newTestReader(t *testing.T, cache map[string]*DownloadRequest, root string) (*imageReader, *fakeClock) {
	t.Helper()
	clock := &fakeClock{now: time.Unix(1700000000, 0)}
	r := &imageReader{
		lookup:  func(u string) (*DownloadRequest, bool) { req, ok := cache[u]; return req, ok },
		now:     clock.Now,
		sleep:   clock.Sleep,
		timeout: 3 * time.Second,
		poll:    500 * time.Millisecond,
		quiet:   imageReadyQuietPeriod,
		pathCheck: func(p string) (bool, error) {
			if !withinRoot(root, p) {
				return false, fmt.Errorf("%w: outside root", ErrImagePathOutsideRoot)
			}
			return cachedImageFileCheck(p)
		},
		writeRoot: func() (string, error) { return root, nil },
	}
	return r, clock
}

func countFiles(t *testing.T, dir string) int {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("read dir: %v", err)
	}
	n := 0
	for _, e := range entries {
		if !e.IsDir() {
			n++
		}
	}
	return n
}

// assertRecoveredPlain 校验恢复出的字节就是合成图片本身。
// 微信 CDN 密文按 AES 块零补齐，解密后可能带 <16 字节尾部零；
// 实现不做裁剪（既不补零伪造、也不丢数据），因此这里允许纯零尾巴。
func assertRecoveredPlain(t *testing.T, got, plain []byte) {
	t.Helper()
	if !bytes.HasPrefix(got, plain) {
		t.Fatalf("恢复内容与合成图片不一致: got=%d plain=%d", len(got), len(plain))
	}
	tail := got[len(plain):]
	if len(tail) >= aes.BlockSize {
		t.Fatalf("尾部补齐异常，超出 AES 块: %d", len(tail))
	}
	for _, b := range tail {
		if b != 0 {
			t.Fatalf("尾部不是零补齐: %x", tail)
		}
	}
}

// --- 纯选择器：只做判断，绝不借 key、绝不跨图片猜 ---------------------------------------

func TestProbeImageCandidatePrefersReadyMid(t *testing.T) {
	mid := "https://cdn.example.com/mid?m=1&aeskey=" + testImageKeyHex
	thumb := "https://cdn.example.com/thumb?m=2"
	now := time.Unix(1700000000, 0)
	cache := map[string]*DownloadRequest{
		mid:   entry(mid, []byte("mid-cipher"), now.Add(-5*time.Second)),
		thumb: entry(thumb, []byte("thumb-cipher"), now.Add(-5*time.Second)),
	}
	r, _ := newTestReader(t, cache, t.TempDir())

	pMid, _, err := r.probeImageCandidate(imageCandidate{quality: imageQualityMid, cdnURL: mid, aesKey: testImageKeyHex, hasKey: true}, now)
	if err != nil || !pMid.complete || pMid.quality != imageQualityMid {
		t.Fatalf("mid probe: %+v err=%v", pMid, err)
	}
	pThumb, _, err := r.probeImageCandidate(imageCandidate{quality: imageQualityThumbnail, cdnURL: thumb, aesKey: testImageKeyHexAlt, hasKey: true}, now)
	if err != nil || !pThumb.complete {
		t.Fatalf("thumb probe: %+v err=%v", pThumb, err)
	}
}

func TestProbeImageCandidateWithoutOwnKeyNeverBorrowsAnotherKey(t *testing.T) {
	thumb := "https://cdn.example.com/thumb?m=2"
	now := time.Unix(1700000000, 0)
	cache := map[string]*DownloadRequest{thumb: entry(thumb, []byte("thumb-cipher"), now.Add(-5*time.Second))}
	r, _ := newTestReader(t, cache, t.TempDir())

	p, _, err := r.probeImageCandidate(imageCandidate{quality: imageQualityThumbnail, cdnURL: thumb, hasKey: false}, now)
	if err != nil {
		t.Fatalf("probe: %v", err)
	}
	if !p.keyMissing || p.complete || p.filePath != "" {
		t.Fatalf("无 thumb key 时必须不可用，实际: %+v", p)
	}
}

func TestProbeImageCandidatePendingUntilQuietPeriod(t *testing.T) {
	url := "https://cdn.example.com/mid?m=1"
	now := time.Unix(1700000000, 0)
	cache := map[string]*DownloadRequest{url: entry(url, []byte("partial"), now.Add(-100*time.Millisecond))}
	r, _ := newTestReader(t, cache, t.TempDir())

	p, _, _ := r.probeImageCandidate(imageCandidate{quality: imageQualityMid, cdnURL: url, aesKey: testImageKeyHex, hasKey: true}, now)
	if p.complete || !p.pending {
		t.Fatalf("仍在追加中的数据不应视为收完: %+v", p)
	}
	// 静默期过后视为收完
	p2, _, _ := r.probeImageCandidate(imageCandidate{quality: imageQualityMid, cdnURL: url, aesKey: testImageKeyHex, hasKey: true}, now.Add(imageReadyQuietPeriod))
	if !p2.complete {
		t.Fatalf("静默期后应视为收完: %+v", p2)
	}
}

func TestImageCandidatesForUsesPerCandidateOwnKey(t *testing.T) {
	cands := imageCandidatesFor(imageMsgURLs{
		MidURL: "https://cdn.example.com/mid?m=1", MidAESKey: testImageKeyHex,
		ThumbURL: "https://cdn.example.com/thumb?m=2", ThumbAESKey: "",
	})
	if len(cands) != 2 || cands[0].quality != imageQualityMid || cands[0].aesKey != testImageKeyHex || !cands[0].hasKey {
		t.Fatalf("中图候选错误: %+v", cands)
	}
	if cands[1].quality != imageQualityThumbnail || cands[1].aesKey != "" || cands[1].hasKey {
		t.Fatalf("缩略图候选必须只用自己的 key: %+v", cands[1])
	}
}

// --- 恢复主流程：真实合成 AES 图片数据离线解密 -------------------------------------------

func TestRecoverImagePathUsesCachedThumbnailWithoutWaitingForMid(t *testing.T) {
	root := t.TempDir()
	plain := pngFixture(t)
	thumb := "https://cdn.example.com/thumb?m=2&aeskey=" + testImageKeyHex
	now := time.Unix(1700000000, 0)
	cache := map[string]*DownloadRequest{
		thumb: entry(thumb, encryptImagePayload(t, testImageKeyHex, plain), now.Add(-5*time.Second)),
	}
	r, clock := newTestReader(t, cache, root)

	path, quality, err := r.recover(imageMsgURLs{
		MidURL: "https://cdn.example.com/mid?m=1", MidAESKey: testImageKeyHex,
		ThumbURL: thumb, ThumbAESKey: testImageKeyHex,
	})
	if err != nil {
		t.Fatalf("recover: %v", err)
	}
	if clock.sleepCount() != 0 {
		t.Fatalf("缩略图已缓存时不应等待中图，sleep 次数=%d", clock.sleepCount())
	}
	if quality != imageQualityThumbnail {
		t.Fatalf("quality=%q", quality)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read recovered: %v", err)
	}
	assertRecoveredPlain(t, got, plain)
	if !withinRoot(root, path) {
		t.Fatalf("恢复文件必须落在 image_path 根目录内: %s", path)
	}
}

func TestRecoverImagePathPrefersMidOverCachedThumbnail(t *testing.T) {
	root := t.TempDir()
	midPlain := jpegFixture(t)
	thumbPlain := pngFixture(t)
	mid := "https://cdn.example.com/mid?m=1"
	thumb := "https://cdn.example.com/thumb?m=2"
	now := time.Unix(1700000000, 0)
	cache := map[string]*DownloadRequest{
		mid:   entry(mid, encryptImagePayload(t, testImageKeyHex, midPlain), now.Add(-5*time.Second)),
		thumb: entry(thumb, encryptImagePayload(t, testImageKeyHexAlt, thumbPlain), now.Add(-5*time.Second)),
	}
	r, clock := newTestReader(t, cache, root)

	path, quality, err := r.recover(imageMsgURLs{MidURL: mid, MidAESKey: testImageKeyHex, ThumbURL: thumb, ThumbAESKey: testImageKeyHexAlt})
	if err != nil {
		t.Fatalf("recover: %v", err)
	}
	if quality != imageQualityMid || clock.sleepCount() != 0 {
		t.Fatalf("quality=%q sleeps=%d", quality, clock.sleepCount())
	}
	got, _ := os.ReadFile(path)
	assertRecoveredPlain(t, got, midPlain)
}

// 中图条目存在但还在接收中 → 立即改用已缓存的缩略图，不允许先等中图。
func TestRecoverImagePathDoesNotBlockOnPendingMidWhenThumbnailReady(t *testing.T) {
	root := t.TempDir()
	thumbPlain := pngFixture(t)
	mid := "https://cdn.example.com/mid?m=1"
	thumb := "https://cdn.example.com/thumb?m=2"
	now := time.Unix(1700000000, 0)
	cache := map[string]*DownloadRequest{
		mid:   entry(mid, []byte("partial-mid"), now.Add(-10*time.Millisecond)),
		thumb: entry(thumb, encryptImagePayload(t, testImageKeyHex, thumbPlain), now.Add(-5*time.Second)),
	}
	r, clock := newTestReader(t, cache, root)

	path, quality, err := r.recover(imageMsgURLs{MidURL: mid, MidAESKey: testImageKeyHex, ThumbURL: thumb, ThumbAESKey: testImageKeyHex})
	if err != nil {
		t.Fatalf("recover: %v", err)
	}
	if quality != imageQualityThumbnail || clock.sleepCount() != 0 {
		t.Fatalf("应立刻使用缩略图: quality=%q sleeps=%d", quality, clock.sleepCount())
	}
	got, _ := os.ReadFile(path)
	assertRecoveredPlain(t, got, thumbPlain)
}

func TestRecoverImagePathTimesOutWithClearErrorAndNoFile(t *testing.T) {
	root := t.TempDir()
	r, clock := newTestReader(t, map[string]*DownloadRequest{}, root)

	path, quality, err := r.recover(imageMsgURLs{
		MidURL: "https://cdn.example.com/mid?m=1", MidAESKey: testImageKeyHex,
		ThumbURL: "https://cdn.example.com/thumb?m=2", ThumbAESKey: testImageKeyHex,
	})
	if !errors.Is(err, ErrImageNotCached) {
		t.Fatalf("期望 ErrImageNotCached，实际 %v", err)
	}
	if path != "" || quality != "" {
		t.Fatalf("失败时不能返回路径/质量: %q %q", path, quality)
	}
	if clock.sleepCount() == 0 {
		t.Fatalf("无缓存条目时应按注入的 clock 等待到超时")
	}
	if n := countFiles(t, root); n != 0 {
		t.Fatalf("失败时不能写文件，实际 %d 个", n)
	}
	if strings.Contains(err.Error(), testImageKeyHex) {
		t.Fatalf("错误信息泄漏 AES key: %v", err)
	}
}

func TestRecoverImagePathRejectsUnsupportedThumbKeylessMessage(t *testing.T) {
	root := t.TempDir()
	thumb := "https://cdn.example.com/thumb?m=2"
	now := time.Unix(1700000000, 0)
	cache := map[string]*DownloadRequest{thumb: entry(thumb, encryptImagePayload(t, testImageKeyHex, pngFixture(t)), now.Add(-5*time.Second))}
	r, _ := newTestReader(t, cache, root)

	_, _, err := r.recover(imageMsgURLs{ThumbURL: thumb, ThumbAESKey: ""})
	if !errors.Is(err, ErrImageThumbKeyMissing) {
		t.Fatalf("无缩略图 key 时必须报 ErrImageThumbKeyMissing，实际 %v", err)
	}
	if n := countFiles(t, root); n != 0 {
		t.Fatalf("不该产出文件，实际 %d", n)
	}
}

func TestRecoverImagePathRejectsUnalignedCipherInsteadOfPadding(t *testing.T) {
	root := t.TempDir()
	mid := "https://cdn.example.com/mid?m=1"
	now := time.Unix(1700000000, 0)
	broken := encryptImagePayload(t, testImageKeyHex, pngFixture(t))
	broken = broken[:len(broken)-3] // 非整块（被截断）
	cache := map[string]*DownloadRequest{mid: entry(mid, broken, now.Add(-5*time.Second))}
	r, _ := newTestReader(t, cache, root)

	_, _, err := r.recover(imageMsgURLs{MidURL: mid, MidAESKey: testImageKeyHex})
	if !errors.Is(err, ErrImageUnalignedCipher) {
		t.Fatalf("期望 ErrImageUnalignedCipher，实际 %v", err)
	}
	if n := countFiles(t, root); n != 0 {
		t.Fatalf("拒绝时不能补零伪造图片，实际写了 %d 个文件", n)
	}
}

func TestRecoverImagePathFallsBackToThumbWhenMidPayloadBroken(t *testing.T) {
	root := t.TempDir()
	thumbPlain := pngFixture(t)
	mid := "https://cdn.example.com/mid?m=1"
	thumb := "https://cdn.example.com/thumb?m=2"
	now := time.Unix(1700000000, 0)
	broken := encryptImagePayload(t, testImageKeyHex, jpegFixture(t))
	broken = broken[:len(broken)-1]
	cache := map[string]*DownloadRequest{
		mid:   entry(mid, broken, now.Add(-5*time.Second)),
		thumb: entry(thumb, encryptImagePayload(t, testImageKeyHexAlt, thumbPlain), now.Add(-5*time.Second)),
	}
	r, _ := newTestReader(t, cache, root)

	path, quality, err := r.recover(imageMsgURLs{MidURL: mid, MidAESKey: testImageKeyHex, ThumbURL: thumb, ThumbAESKey: testImageKeyHexAlt})
	if err != nil {
		t.Fatalf("中图坏数据时应回退缩略图: %v", err)
	}
	if quality != imageQualityThumbnail {
		t.Fatalf("quality=%q", quality)
	}
	got, _ := os.ReadFile(path)
	assertRecoveredPlain(t, got, thumbPlain)
}

func TestRecoverImagePathRejectsDecryptedNonImage(t *testing.T) {
	root := t.TempDir()
	mid := "https://cdn.example.com/mid?m=1"
	now := time.Unix(1700000000, 0)
	junk := encryptImagePayload(t, testImageKeyHex, bytes.Repeat([]byte("not-an-image-at-all!"), 4))
	cache := map[string]*DownloadRequest{mid: entry(mid, junk, now.Add(-5*time.Second))}
	r, _ := newTestReader(t, cache, root)

	path, _, err := r.recover(imageMsgURLs{MidURL: mid, MidAESKey: testImageKeyHex})
	if !errors.Is(err, ErrImageNotImage) {
		t.Fatalf("期望 ErrImageNotImage，实际 %v", err)
	}
	if path != "" || countFiles(t, root) != 0 {
		t.Fatalf("绝不能把非图片内容交给模型: path=%q files=%d", path, countFiles(t, root))
	}
}

func TestRecoverImagePathRejectsOversizedPayload(t *testing.T) {
	root := t.TempDir()
	mid := "https://cdn.example.com/mid?m=1"
	now := time.Unix(1700000000, 0)
	big := make([]byte, maxRecoveredImageBytes+aes.BlockSize) // 整块对齐但超限
	cache := map[string]*DownloadRequest{mid: entry(mid, big, now.Add(-5*time.Second))}
	r, _ := newTestReader(t, cache, root)

	_, _, err := r.recover(imageMsgURLs{MidURL: mid, MidAESKey: testImageKeyHex})
	if !errors.Is(err, ErrImageTooLarge) {
		t.Fatalf("期望 ErrImageTooLarge，实际 %v", err)
	}
	if countFiles(t, root) != 0 {
		t.Fatalf("超限不应产出文件")
	}
}

func TestRecoverImagePathDecryptsWebpWithTrailingPadding(t *testing.T) {
	root := t.TempDir()
	plain := webpFixture(t)
	thumb := "https://cdn.example.com/thumb?m=9"
	now := time.Unix(1700000000, 0)
	cache := map[string]*DownloadRequest{thumb: entry(thumb, encryptImagePayload(t, testImageKeyHex, plain), now.Add(-5*time.Second))}
	r, _ := newTestReader(t, cache, root)

	path, quality, err := r.recover(imageMsgURLs{ThumbURL: thumb, ThumbAESKey: testImageKeyHex})
	if err != nil {
		t.Fatalf("webp 不能被默默丢弃: %v", err)
	}
	if quality != imageQualityThumbnail || !strings.HasSuffix(path, ".webp") {
		t.Fatalf("quality=%q path=%q", quality, path)
	}
	got, _ := os.ReadFile(path)
	if !bytes.HasPrefix(got, []byte("RIFF")) || !bytes.Contains(got[:16], []byte("WEBP")) {
		t.Fatalf("webp magic 校验失败")
	}
}

// --- 缓存 FilePath 的校验：范围 + 配对 + 真实图片 ---------------------------------------

func TestRecoverImagePathRejectsCachedFilePathOutsideImagePath(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()
	outsideFile := filepath.Join(outside, "outside.png")
	if err := os.WriteFile(outsideFile, pngFixture(t), 0o644); err != nil {
		t.Fatal(err)
	}
	thumb := "https://cdn.example.com/thumb?m=2"
	now := time.Unix(1700000000, 0)
	cache := map[string]*DownloadRequest{
		thumb: {CDNURL: thumb, FilePath: outsideFile, LastAppendTime: now.UnixMilli()},
	}
	r, _ := newTestReader(t, cache, root)

	path, _, err := r.recover(imageMsgURLs{ThumbURL: thumb, ThumbAESKey: testImageKeyHex})
	if !errors.Is(err, ErrImagePathOutsideRoot) {
		t.Fatalf("必须拒绝 image_path 范围外的缓存路径，实际 %v", err)
	}
	if path == outsideFile || path != "" {
		t.Fatalf("绝不能返回范围外路径: %q", path)
	}
}

func TestRecoverImagePathRejectsCachedNonImageFilePath(t *testing.T) {
	root := t.TempDir()
	notImage := filepath.Join(root, "wechat.dat")
	if err := os.WriteFile(notImage, []byte("plain chat text, not an image"), 0o644); err != nil {
		t.Fatal(err)
	}
	thumb := "https://cdn.example.com/thumb?m=2"
	now := time.Unix(1700000000, 0)
	cache := map[string]*DownloadRequest{thumb: {CDNURL: thumb, FilePath: notImage, LastAppendTime: now.UnixMilli()}}
	r, _ := newTestReader(t, cache, root)

	path, _, err := r.recover(imageMsgURLs{ThumbURL: thumb, ThumbAESKey: testImageKeyHex})
	if !errors.Is(err, ErrImageNotImage) {
		t.Fatalf("范围内但非图片的缓存文件也必须拒绝，实际 %v", err)
	}
	if path != "" {
		t.Fatalf("不能返回非图片文件: %q", path)
	}
}

func TestRecoverImagePathUsesValidCachedFilePathWithoutDecrypting(t *testing.T) {
	root := t.TempDir()
	plain := pngFixture(t)
	inside := filepath.Join(root, "cached.png")
	if err := os.WriteFile(inside, plain, 0o644); err != nil {
		t.Fatal(err)
	}
	thumb := "https://cdn.example.com/thumb?m=2"
	now := time.Unix(1700000000, 0)
	// 缓存里的 Media 是垃圾：若实现去解密就会失败，说明它真的复用了已落盘文件。
	cache := map[string]*DownloadRequest{
		thumb: {CDNURL: thumb, FilePath: inside, Media: []byte("garbage-cipher"), LastAppendTime: now.UnixMilli()},
	}
	r, clock := newTestReader(t, cache, root)

	path, quality, err := r.recover(imageMsgURLs{ThumbURL: thumb, ThumbAESKey: testImageKeyHex})
	if err != nil {
		t.Fatalf("复用合法缓存路径: %v", err)
	}
	if path != inside || quality != imageQualityThumbnail || clock.sleepCount() != 0 {
		t.Fatalf("path=%q quality=%q sleeps=%d", path, quality, clock.sleepCount())
	}
	if countFiles(t, root) != 1 {
		t.Fatalf("复用已有文件时不应再写新文件")
	}
}

func TestRecoverImagePathRejectsCDNEntryMismatch(t *testing.T) {
	root := t.TempDir()
	thumb := "https://cdn.example.com/thumb?m=2"
	other := "https://cdn.example.com/thumb?m=999"
	now := time.Unix(1700000000, 0)
	cache := map[string]*DownloadRequest{
		thumb: entry(other, encryptImagePayload(t, testImageKeyHex, pngFixture(t)), now.Add(-5*time.Second)),
	}
	r, clock := newTestReader(t, cache, root)

	_, _, err := r.recover(imageMsgURLs{ThumbURL: thumb, ThumbAESKey: testImageKeyHex})
	if !errors.Is(err, ErrImageCDNMismatch) {
		t.Fatalf("期望 ErrImageCDNMismatch，实际 %v", err)
	}
	if clock.sleepCount() != 0 || countFiles(t, root) != 0 {
		t.Fatalf("CDN 不配对必须立即失败且不写文件")
	}
}

func TestRecoverImagePathErrorsNeverLeakAESKey(t *testing.T) {
	root := t.TempDir()
	mid := "https://cdn.example.com/mid?m=1&aeskey=" + testImageKeyHex
	now := time.Unix(1700000000, 0)
	cases := []struct {
		name string
		req  *DownloadRequest
	}{
		{"unaligned", entry(mid, []byte("0123456789abcde"), now.Add(-5*time.Second))},
		{"non-image", entry(mid, encryptImagePayload(t, testImageKeyHex, bytes.Repeat([]byte("x"), 32)), now.Add(-5*time.Second))},
	}
	for _, c := range cases {
		cache := map[string]*DownloadRequest{mid: c.req}
		r, _ := newTestReader(t, cache, root)
		_, _, err := r.recover(imageMsgURLs{MidURL: mid, MidAESKey: testImageKeyHex})
		if err == nil {
			t.Fatalf("%s: 期望错误", c.name)
		}
		if strings.Contains(err.Error(), testImageKeyHex) || strings.Contains(err.Error(), "aeskey=") {
			t.Fatalf("%s: 错误信息泄漏 AES key/URL: %v", c.name, err)
		}
	}
}

// --- magic 校验 ---------------------------------------------------------------------

func TestSniffSupportedImageAcceptsRealFormats(t *testing.T) {
	for name, data := range map[string][]byte{
		"png":  pngFixture(t),
		"jpg":  jpegFixture(t),
		"webp": webpFixture(t),
	} {
		ext, err := sniffSupportedImage(data)
		if err != nil || ext != name {
			t.Fatalf("%s: ext=%q err=%v", name, ext, err)
		}
	}
	if _, err := sniffSupportedImage([]byte("not an image at all")); !errors.Is(err, ErrImageNotImage) {
		t.Fatalf("非图片必须报错: %v", err)
	}
	if _, err := sniffSupportedImage([]byte("RIFF\x00\x00\x00\x00AVI ")); !errors.Is(err, ErrImageNotImage) {
		t.Fatalf("RIFF 但非 WEBP 必须报错: %v", err)
	}
}

func TestVerifyImageHeaderRejectsTruncatedPng(t *testing.T) {
	plain := pngFixture(t)
	truncated := plain[:20] // 只有签名 + 半个 IHDR
	if err := verifyImageHeader(truncated, "png"); err == nil {
		t.Fatalf("截断的 PNG 头部必须被拒绝")
	}
}

// --- HandleMsg 接入 -----------------------------------------------------------------

func imageXML(attrs string) string {
	return `<?xml version="1.0"?><msg><img ` + attrs + ` /></msg>`
}

func handleImageMessage(t *testing.T, content string) *WechatMessage {
	t.Helper()
	data := &wxproto.WxRecvMsgData{
		Sender:      &wxproto.WxString{Value: "wxid_peer_fixture"},
		Receiver:    &wxproto.WxString{Value: "wxid_self_fixture"},
		Content:     &wxproto.WxString{Value: content},
		ContentType: 1,
		MsgId:       4242,
	}
	raw, err := buildWechatMessageJSON(data)
	if err != nil {
		t.Fatalf("buildWechatMessageJSON: %v", err)
	}
	out, err := HandleMsg(raw)
	if err != nil {
		t.Fatalf("HandleMsg: %v", err)
	}
	var m WechatMessage
	if err := json.Unmarshal(out, &m); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return &m
}

func TestHandleMsgImageBranchMarksThumbnailData(t *testing.T) {
	root := t.TempDir()
	oldPath := config.ImagePath
	config.ImagePath = root + string(os.PathSeparator)
	defer func() { config.ImagePath = oldPath }()

	plain := pngFixture(t)
	mid := "https://cdn.example.com/mid?m=1"
	thumb := "https://cdn.example.com/thumb?m=2"
	userID2FileMsgMap.Store(thumb, entry(thumb, encryptImagePayload(t, testImageKeyHex, plain), time.Now().Add(-5*time.Second)))
	defer userID2FileMsgMap.Delete(thumb)

	m := handleImageMessage(t, imageXML(fmt.Sprintf(
		`aeskey="%s" cdnmidimgurl="%s" cdnthumbaeskey="%s" cdnthumburl="%s" length="1234" md5="deadbeef"`,
		testImageKeyHex, mid, testImageKeyHex, thumb)))

	if len(m.Message) != 1 || m.Message[0].Type != "image" {
		t.Fatalf("消息路由被改动: %+v", m.Message)
	}
	d := m.Message[0].Data
	if d == nil || !strings.HasPrefix(d.URL, "file://") {
		t.Fatalf("缺少 file:// URL: %+v", d)
	}
	if d.ImageQuality != imageQualityThumbnail {
		t.Fatalf("使用缩略图时必须标注 image_quality=thumbnail，实际 %q", d.ImageQuality)
	}
	// 参与者字段修复不能被回退
	if m.FromUserID != "wxid_peer_fixture" || m.ToUserID != "wxid_self_fixture" {
		t.Fatalf("参与者信息丢失: %+v", m)
	}
	// 原文与 key 不能被改写/泄漏到新增字段
	if !strings.Contains(m.RawMessage, "cdnthumburl") || !strings.Contains(m.MsgResource, "cdnmidimgurl") && m.MsgResource != "" {
		t.Fatalf("原始 XML 必须保持原样")
	}
	got, err := os.ReadFile(strings.TrimPrefix(d.URL, "file://"))
	if err != nil {
		t.Fatalf("读取恢复图片: %v", err)
	}
	assertRecoveredPlain(t, got, plain)
}

func TestHandleMsgImageBranchMidHasNoImageQualityField(t *testing.T) {
	root := t.TempDir()
	oldPath := config.ImagePath
	config.ImagePath = root + string(os.PathSeparator)
	defer func() { config.ImagePath = oldPath }()

	plain := jpegFixture(t)
	mid := "https://cdn.example.com/mid?m=1"
	userID2FileMsgMap.Store(mid, entry(mid, encryptImagePayload(t, testImageKeyHex, plain), time.Now().Add(-5*time.Second)))
	defer userID2FileMsgMap.Delete(mid)

	m := handleImageMessage(t, imageXML(fmt.Sprintf(
		`aeskey="%s" cdnmidimgurl="%s" cdnthumbaeskey="%s" cdnthumburl="%s"`,
		testImageKeyHex, mid, testImageKeyHexAlt, "https://cdn.example.com/thumb?m=2")))

	d := m.Message[0].Data
	if d.ImageQuality != "" {
		t.Fatalf("中图不应标注 image_quality，实际 %q", d.ImageQuality)
	}
	raw, _ := json.Marshal(d)
	if strings.Contains(string(raw), "image_quality") {
		t.Fatalf("中图路径不应出现 image_quality 字段: %s", raw)
	}
	got, _ := os.ReadFile(strings.TrimPrefix(d.URL, "file://"))
	assertRecoveredPlain(t, got, plain)
}

func TestHandleMsgImageBranchFailureReturnsErrorWithoutNonImagePath(t *testing.T) {
	root := t.TempDir()
	oldPath := config.ImagePath
	config.ImagePath = root + string(os.PathSeparator)
	oldReader := defaultImageReader
	r, _ := newTestReader(t, map[string]*DownloadRequest{}, root)
	defaultImageReader = r
	defer func() {
		config.ImagePath = oldPath
		defaultImageReader = oldReader
	}()

	content := imageXML(`aeskey="` + testImageKeyHex + `" cdnmidimgurl="https://cdn.example.com/mid?m=1" cdnthumbaeskey="` + testImageKeyHex + `" cdnthumburl="https://cdn.example.com/thumb?m=2"`)
	data := &wxproto.WxRecvMsgData{
		Sender:      &wxproto.WxString{Value: "wxid_peer_fixture"},
		Receiver:    &wxproto.WxString{Value: "wxid_self_fixture"},
		Content:     &wxproto.WxString{Value: content},
		ContentType: 1,
		MsgId:       4243,
	}
	raw, err := buildWechatMessageJSON(data)
	if err != nil {
		t.Fatalf("buildWechatMessageJSON: %v", err)
	}
	out, err := HandleMsg(raw)
	if err == nil {
		t.Fatalf("缓存不可用时必须返回明确错误，而不是非图片路径")
	}
	if out != nil {
		t.Fatalf("失败时不应下发消息 JSON")
	}
	if strings.Contains(err.Error(), testImageKeyHex) || strings.Contains(err.Error(), "aeskey") {
		t.Fatalf("错误信息泄漏 key/URL: %v", err)
	}
	if errors.Is(err, ErrImageNotImage) || !errors.Is(err, ErrImageNotCached) {
		t.Fatalf("错误类型应清晰可辨: %v", err)
	}
	if countFiles(t, root) != 0 {
		t.Fatalf("失败时不写文件")
	}
}

func TestRecoverImagePathPublicEntryUsesDefaultReader(t *testing.T) {
	root := t.TempDir()
	oldPath := config.ImagePath
	config.ImagePath = root + string(os.PathSeparator)
	defer func() { config.ImagePath = oldPath }()

	plain := pngFixture(t)
	mid := "https://cdn.example.com/mid?m=1"
	userID2FileMsgMap.Store(mid, entry(mid, encryptImagePayload(t, testImageKeyHex, plain), time.Now().Add(-5*time.Second)))
	defer userID2FileMsgMap.Delete(mid)

	path, quality, err := RecoverImagePath(imageMsgURLs{MidURL: mid, MidAESKey: testImageKeyHex})
	if err != nil {
		t.Fatalf("RecoverImagePath: %v", err)
	}
	if quality != imageQualityMid || !withinRoot(root, path) {
		t.Fatalf("quality=%q path=%q", quality, path)
	}
}

// --- 落盘权限：目录 0700、文件 0600 ---------------------------------------------------

func TestWriteImageUsesPrivatePermissions(t *testing.T) {
	base := t.TempDir()
	root := filepath.Join(base, "nested", "image")
	r, _ := newTestReader(t, nil, root)
	r.writeRoot = func() (string, error) { return root, nil }

	path, err := r.writeImage("png", pngFixture(t))
	if err != nil {
		t.Fatalf("writeImage: %v", err)
	}
	fi, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat file: %v", err)
	}
	if perm := fi.Mode().Perm(); perm != imageWriteFilePerm {
		t.Fatalf("图片文件权限应为 0600，实际 %o", perm)
	}
	di, err := os.Stat(filepath.Dir(path))
	if err != nil {
		t.Fatalf("stat dir: %v", err)
	}
	if perm := di.Mode().Perm(); perm != imageWriteDirPerm {
		t.Fatalf("图片目录权限应为 0700，实际 %o", perm)
	}
}

// 生产场景：config.ImagePath 指向账号私有目录，而 JS 给的 file_path 在微信容器里（范围外）
// → 必须拒绝该路径，并回退到用被动缓存密文 + 该候选自己的 key 解密落盘。
func TestRecoverImagePathFallsBackToDecryptWhenCachedPathOutsideRoot(t *testing.T) {
	root := t.TempDir()       // 新的账号私有 image 目录
	wechatLike := t.TempDir() // 模拟微信 container/temp/.../Img
	outsideFile := filepath.Join(wechatLike, "deadbeef.dat")
	if err := os.WriteFile(outsideFile, []byte("opaque container file"), 0o644); err != nil {
		t.Fatal(err)
	}
	plain := pngFixture(t)
	thumb := "https://cdn.example.com/thumb?m=7"
	now := time.Unix(1700000000, 0)
	cache := map[string]*DownloadRequest{
		thumb: {
			CDNURL:         thumb,
			FilePath:       outsideFile,
			Media:          encryptImagePayload(t, testImageKeyHex, plain),
			LastAppendTime: now.Add(-5 * time.Second).UnixMilli(),
		},
	}
	r, _ := newTestReader(t, cache, root)

	path, quality, err := r.recover(imageMsgURLs{ThumbURL: thumb, ThumbAESKey: testImageKeyHex})
	if err != nil {
		t.Fatalf("范围外缓存路径应回退解密而非直接失败: %v", err)
	}
	if path == outsideFile || !withinRoot(root, path) || quality != imageQualityThumbnail {
		t.Fatalf("path=%q quality=%q", path, quality)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	assertRecoveredPlain(t, got, plain)
}
