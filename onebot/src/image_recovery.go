package main

// 微信图片被动缓存恢复（最小候选实现）。
//
// 设计约束（对应稳定微信图片恢复的需求）：
//   - 只读被动下载缓存 userID2FileMsgMap，绝不主动触发 native 下载 / 伪造 native 任务；
//   - 优先该消息自身的「中图」缓存，其次该消息自身的「缩略图」缓存，每条候选只用自己的 key，
//     绝不跨图片/跨候选借 key（无 thumb key 就不猜）；
//   - 先探测再等待：缩略图已缓存时立即使用，不会先干等中图超时；
//   - 拒绝不完整 AES 块（不补零伪造图片）；解密结果必须是真实 PNG/JPEG/WEBP magic；
//   - 单张 ≤ 8MiB；失败明确报错，绝不把非图片 bin 交给模型；
//   - 复用缓存里已有的 FilePath 时，必须落在当前 config.ImagePath 根目录内、与该 CDN 条目配对，
//     且文件内容确实是图片，否则拒绝读取任意路径；
//   - 任何日志/错误信息都不包含 AES key（CDN URL 的 query 里也可能带 aeskey，只回显 host）。

import (
	"bytes"
	"crypto/aes"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"image"
	_ "image/jpeg" // 注册 JPEG 解码器，用于头部可解析性校验
	_ "image/png"  // 注册 PNG 解码器，用于头部可解析性校验
	"io"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"time"
)

const (
	// maxRecoveredImageBytes 单张图片（密文/明文）允许的最大字节数：8MiB。
	maxRecoveredImageBytes = 8 * 1024 * 1024

	// 图片质量标识：中图可当作原图处理；缩略图必须在消息里明确标注，
	// 供 Python 侧说明「这是缩略图，不是原图」，避免冒充原图。
	imageQualityMid       = "mid"
	imageQualityThumbnail = "thumbnail"

	// 判断被动缓存里某条数据「已经收完」的静默期（与既有下载逻辑保持一致）。
	imageReadyQuietPeriod = 2 * time.Second
	// 默认等待预算：中图/缩略图都没准备好时最多等这么久（远小于原先 60s）。
	imageWaitTimeout = 30 * time.Second
	// 轮询间隔：每次轮询都会同时检查中图与缩略图。
	imageWaitPoll = 500 * time.Millisecond
	// 校验缓存文件内容时只读的头部字节数。
	imageMagicProbeBytes = 32

	imageWriteDirPerm  = 0o700
	imageWriteFilePerm = 0o600
)

// 明确的错误类型，便于调用方/Python 侧区分「超时」「坏图」「格式不支持」「越权路径」。
var (
	ErrImageNotCached       = errors.New("image: 中图/缩略图被动缓存尚未就绪（没有完整数据）")
	ErrImageThumbKeyMissing = errors.New("image: 该消息没有缩略图 AES key，拒绝改用其他 key 或其他图片")
	ErrImageKeyInvalid      = errors.New("image: AES key 非法（非 hex 或长度不是 16/24/32 字节）")
	ErrImageTooLarge        = errors.New("image: 数据超过 8MiB 上限")
	ErrImageUnalignedCipher = errors.New("image: 密文不是完整 AES 块，拒绝补零伪造图片")
	ErrImageNotImage        = errors.New("image: 数据不是受支持的图片(PNG/JPEG/WEBP)，不返回非图片内容")
	ErrImagePathOutsideRoot = errors.New("image: 缓存文件路径不在当前 image_path 范围内，拒绝读取")
	ErrImageCDNMismatch     = errors.New("image: 缓存条目与当前消息的 CDN 地址不配对")
	ErrImageWriteFailed     = errors.New("image: 图片写入失败")
)

// imageMsgURLs 是一条图片消息自身的候选来源（全部来自该消息 XML，不跨消息猜）。
type imageMsgURLs struct {
	MidURL      string // <img cdnmidimgurl>
	MidAESKey   string // <img aeskey>（中图专用）
	ThumbURL    string // <img cdnthumburl>
	ThumbAESKey string // <img cdnthumbaeskey>（缩略图专用）
}

// imageCandidate 一条候选：只带自己的 URL 与自己的 key。
type imageCandidate struct {
	quality string
	cdnURL  string
	aesKey  string
	hasKey  bool
}

// imageCandidatesFor 按优先级（中图 → 缩略图）列出候选；key 严格配对，不填充、不外借。
func imageCandidatesFor(msg imageMsgURLs) []imageCandidate {
	var out []imageCandidate
	if msg.MidURL != "" {
		out = append(out, imageCandidate{
			quality: imageQualityMid,
			cdnURL:  msg.MidURL,
			aesKey:  strings.TrimSpace(msg.MidAESKey),
			hasKey:  strings.TrimSpace(msg.MidAESKey) != "",
		})
	}
	if msg.ThumbURL != "" {
		out = append(out, imageCandidate{
			quality: imageQualityThumbnail,
			cdnURL:  msg.ThumbURL,
			aesKey:  strings.TrimSpace(msg.ThumbAESKey),
			hasKey:  strings.TrimSpace(msg.ThumbAESKey) != "",
		})
	}
	return out
}

// downloadSnapshot 在锁保护下取到的缓存条目快照（不复制大块 Media）。
type downloadSnapshot struct {
	mediaLen   int
	filePath   string
	lastAppend int64
	cdnURL     string
}

func peekDownloadEntry(req *DownloadRequest) downloadSnapshot {
	req.mu.Lock()
	defer req.mu.Unlock()
	return downloadSnapshot{
		mediaLen:   len(req.Media),
		filePath:   req.FilePath,
		lastAppend: req.LastAppendTime,
		cdnURL:     req.CDNURL,
	}
}

func takeDownloadMedia(req *DownloadRequest) []byte {
	req.mu.Lock()
	defer req.mu.Unlock()
	if len(req.Media) == 0 {
		return nil
	}
	out := make([]byte, len(req.Media))
	copy(out, req.Media)
	return out
}

// rememberImagePath 把本次成功恢复的路径记回缓存条目（与既有下载行为一致，加速重复图片）。
func rememberImagePath(req *DownloadRequest, path string) {
	if req == nil || path == "" {
		return
	}
	req.mu.Lock()
	defer req.mu.Unlock()
	if req.FilePath == "" {
		req.FilePath = path
		req.Media = nil // The validated private file is now the cache; release ciphertext.
	}
}

// imageCacheProbe 一次候选探测的结论（纯数据，便于单测）。
type imageCacheProbe struct {
	quality          string
	entryFound       bool
	keyMissing       bool
	complete         bool // 数据已收完，可以解密
	filePath         string
	filePathRejected error
	pending          bool
}

// imageReader 可注入依赖的读取器：clock/sleeper/缓存查找/路径校验/写入目录都能替换，
// 因此单测可以零等待、纯离线（合成 AES 图片数据）。
type imageReader struct {
	lookup    func(cdnURL string) (*DownloadRequest, bool)
	now       func() time.Time
	sleep     func(time.Duration)
	timeout   time.Duration
	poll      time.Duration
	quiet     time.Duration
	pathCheck func(path string) (bool, error)
	writeRoot func() (string, error)
}

var defaultImageReader = &imageReader{
	lookup:    defaultImageCacheLookup,
	now:       time.Now,
	sleep:     time.Sleep,
	timeout:   imageWaitTimeout,
	poll:      imageWaitPoll,
	quiet:     imageReadyQuietPeriod,
	pathCheck: defaultCachedImagePathCheck,
	writeRoot: defaultImageWriteRoot,
}

func defaultImageCacheLookup(cdnURL string) (*DownloadRequest, bool) {
	if cdnURL == "" {
		return nil, false
	}
	value, ok := userID2FileMsgMap.Load(cdnURL)
	if !ok {
		return nil, false
	}
	req, ok := value.(*DownloadRequest)
	if !ok || req == nil {
		return nil, false
	}
	return req, true
}

// RecoverImagePath 是图片分支的唯一入口：返回 (本地图片路径, 质量, error)。
// 质量取值 imageQualityMid / imageQualityThumbnail；出错时路径为空，绝不返回非图片内容。
// 只读被动缓存，不会调用任何 native 下载接口。
func RecoverImagePath(msg imageMsgURLs) (string, string, error) {
	return defaultImageReader.recover(msg)
}

func (r *imageReader) recover(msg imageMsgURLs) (string, string, error) {
	cands := imageCandidatesFor(msg)
	if len(cands) == 0 {
		return "", "", fmt.Errorf("%w: 消息里既没有 cdnmidimgurl 也没有 cdnthumburl", ErrImageNotCached)
	}
	// poll 非法时兜底，避免忙等。
	poll := r.poll
	if poll <= 0 {
		poll = imageWaitPoll
	}

	deadline := r.now().Add(r.timeout)
	var (
		sawEntry        bool
		pendingAny      bool
		thumbKeyMissing bool
		rejectionErr    error
		readyErr        error
	)

	for {
		at := r.now()
		pendingAny = false
		readyErr = nil

		for _, cand := range cands {
			probe, req, err := r.probeImageCandidate(cand, at)
			if err != nil {
				// CDN 条目与当前消息不配对：立即失败，避免读到别的图片。
				return "", "", err
			}
			if probe.entryFound {
				sawEntry = true
			}
			if probe.keyMissing {
				thumbKeyMissing = true
			}
			if probe.filePathRejected != nil && rejectionErr == nil {
				rejectionErr = probe.filePathRejected
			}
			if probe.pending {
				pendingAny = true
			}
			if probe.filePath != "" {
				return probe.filePath, probe.quality, nil
			}
			if probe.complete {
				path, err := r.materialize(cand, takeDownloadMedia(req))
				if err != nil {
					// 该候选数据坏了：记下来，先看下一条候选（例如中图坏→用缩略图）。
					if readyErr == nil {
						readyErr = err
					}
					continue
				}
				rememberImagePath(req, path)
				return path, probe.quality, nil
			}
		}

		// 有候选数据已收完但都是坏图，且没有其他候选还在接收 → 立刻清楚报错。
		if readyErr != nil && !pendingAny {
			return "", "", readyErr
		}
		if !at.Before(deadline) {
			break
		}
		r.sleep(poll)
	}

	switch {
	case readyErr != nil:
		return "", "", readyErr
	case rejectionErr != nil:
		return "", "", rejectionErr
	case thumbKeyMissing && !sawEntry:
		return "", "", fmt.Errorf("%w: %s", ErrImageThumbKeyMissing, describeCandidates(cands))
	case sawEntry:
		return "", "", fmt.Errorf("%w: 等待 %s 后仍没有完整数据（%s）", ErrImageNotCached, r.timeout, describeCandidates(cands))
	default:
		return "", "", fmt.Errorf("%w: %s", ErrImageNotCached, describeCandidates(cands))
	}
}

// probeImageCandidate 只做判断：不等待、不 IO（除路径校验）、绝不借 key。
func (r *imageReader) probeImageCandidate(cand imageCandidate, at time.Time) (imageCacheProbe, *DownloadRequest, error) {
	probe := imageCacheProbe{quality: cand.quality}
	if cand.cdnURL == "" {
		return probe, nil, nil
	}
	if !cand.hasKey {
		// 该候选自己没有 key：不接受，也不用别的候选的 key 顶替。
		probe.keyMissing = true
		return probe, nil, nil
	}
	req, ok := r.lookup(cand.cdnURL)
	if !ok || req == nil {
		return probe, nil, nil
	}
	probe.entryFound = true

	snap := peekDownloadEntry(req)
	if snap.cdnURL != "" && snap.cdnURL != cand.cdnURL {
		return probe, req, fmt.Errorf("%w: host=%s", ErrImageCDNMismatch, safeCDNLabel(cand.cdnURL))
	}

	if snap.filePath != "" {
		allowed, err := r.pathCheck(snap.filePath)
		if allowed {
			probe.filePath = snap.filePath
			return probe, req, nil
		}
		if err == nil {
			err = fmt.Errorf("%w", ErrImagePathOutsideRoot)
		}
		// 不读范围外/非图片的缓存路径；但同一条 CDN 条目里的密文仍可用（同一条消息、同一个 key）继续判断。
		probe.filePathRejected = err
	}

	if snap.mediaLen > 0 {
		if at.Sub(time.UnixMilli(snap.lastAppend)) >= r.quiet {
			probe.complete = true
		} else {
			probe.pending = true
		}
	}
	return probe, req, nil
}

// materialize 解密 + 校验 + 落盘。任何一步失败都返回明确错误且不产出文件。
func (r *imageReader) materialize(cand imageCandidate, media []byte) (string, error) {
	if len(media) == 0 {
		return "", fmt.Errorf("%w: host=%s", ErrImageNotCached, safeCDNLabel(cand.cdnURL))
	}
	if len(media) > maxRecoveredImageBytes {
		return "", fmt.Errorf("%w: cipher_len=%d", ErrImageTooLarge, len(media))
	}
	plain, err := decryptImageECB(media, cand.aesKey)
	if err != nil {
		return "", err
	}
	if len(plain) > maxRecoveredImageBytes {
		return "", fmt.Errorf("%w: plain_len=%d", ErrImageTooLarge, len(plain))
	}
	ext, err := sniffSupportedImage(plain)
	if err != nil {
		return "", err
	}
	if err := verifyImageHeader(plain, ext); err != nil {
		return "", err
	}
	path, err := r.writeImage(ext, plain)
	if err != nil {
		return "", err
	}
	Info("微信图片被动恢复成功", "quality", cand.quality, "ext", ext, "bytes", len(plain))
	return path, nil
}

// decryptImageECB 用该候选自身的 key 做 AES-ECB 解密。
// 密文不是完整块时直接报错——不补零、不伪造。
func decryptImageECB(media []byte, aesKeyHex string) ([]byte, error) {
	if len(media) == 0 {
		return nil, fmt.Errorf("%w: 空数据", ErrImageNotCached)
	}
	if len(media)%aes.BlockSize != 0 {
		return nil, fmt.Errorf("%w: cipher_len=%d block_size=%d", ErrImageUnalignedCipher, len(media), aes.BlockSize)
	}
	key, err := hex.DecodeString(strings.TrimSpace(aesKeyHex))
	if err != nil {
		return nil, fmt.Errorf("%w: 不是合法 hex", ErrImageKeyInvalid)
	}
	if l := len(key); l != 16 && l != 24 && l != 32 {
		return nil, fmt.Errorf("%w: key_bytes=%d", ErrImageKeyInvalid, l)
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrImageKeyInvalid, err)
	}
	out := make([]byte, len(media))
	for i := 0; i < len(media); i += aes.BlockSize {
		block.Decrypt(out[i:i+aes.BlockSize], media[i:i+aes.BlockSize])
	}
	return out, nil
}

// sniffSupportedImage 只认解密后真实 magic：PNG / JPEG / WEBP。
func sniffSupportedImage(data []byte) (string, error) {
	switch {
	case len(data) >= 8 && bytes.HasPrefix(data, []byte{0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A}):
		return "png", nil
	case len(data) >= 3 && bytes.HasPrefix(data, []byte{0xFF, 0xD8, 0xFF}):
		return "jpg", nil
	case isWebPContainer(data):
		return "webp", nil
	}
	return "", fmt.Errorf("%w: magic=%s", ErrImageNotImage, magicHeadHex(data))
}

func isWebPContainer(data []byte) bool {
	if len(data) < 16 || !bytes.HasPrefix(data, []byte("RIFF")) || !bytes.Equal(data[8:12], []byte("WEBP")) {
		return false
	}
	switch string(data[12:16]) {
	case "VP8 ", "VP8L", "VP8X":
		return true
	}
	return false
}

// verifyImageHeader 在 magic 之上再做一次头部校验：
//   - PNG/JPEG：标准库 image.DecodeConfig 能解析头部（截断/伪造头部会被拒绝）；
//   - WEBP：项目未引入 golang.org/x/image/webp，只能做容器结构校验。
//     这里显式记录该限制并仍然接受 WEBP（绝不静默丢弃 WEBP）。
func verifyImageHeader(data []byte, ext string) error {
	switch ext {
	case "png", "jpg":
		cfg, _, err := image.DecodeConfig(bytes.NewReader(data))
		if err != nil {
			return fmt.Errorf("%w: %s 头部不可解析: %v", ErrImageNotImage, ext, err)
		}
		if cfg.Width <= 0 || cfg.Height <= 0 {
			return fmt.Errorf("%w: %s 尺寸非法 (%dx%d)", ErrImageNotImage, ext, cfg.Width, cfg.Height)
		}
		return nil
	case "webp":
		size := int(binary.LittleEndian.Uint32(data[4:8]))
		if size < 12 || size+8 > len(data) {
			return fmt.Errorf("%w: webp RIFF size=%d data_len=%d", ErrImageNotImage, size, len(data))
		}
		Info("WEBP 仅通过容器结构校验（项目无 golang.org/x/image/webp 解码依赖）", "bytes", len(data))
		return nil
	}
	return fmt.Errorf("%w: 未知扩展名 %q", ErrImageNotImage, ext)
}

var imageWriteSeq int64

// writeImage 把校验通过的图片写入 config.ImagePath 根目录（无配置则回退到可执行文件同级 image/）。
// 目录 0700、文件 0600。
func (r *imageReader) writeImage(ext string, data []byte) (string, error) {
	root, err := r.writeRoot()
	if err != nil {
		return "", err
	}
	root = filepath.Clean(root)
	if err := os.MkdirAll(root, imageWriteDirPerm); err != nil {
		return "", fmt.Errorf("%w: 创建目录失败: %v", ErrImageWriteFailed, err)
	}
	name := fmt.Sprintf("wxrecv_%d_%d.%s", r.now().Unix(), atomic.AddInt64(&imageWriteSeq, 1), ext)
	target := filepath.Join(root, name)
	if err := os.WriteFile(target, data, imageWriteFilePerm); err != nil {
		return "", fmt.Errorf("%w: %v", ErrImageWriteFailed, err)
	}
	// 落盘后必须仍然在允许的根目录内（防御路径穿越）。
	if !withinRoot(root, target) {
		_ = os.Remove(target)
		return "", fmt.Errorf("%w: %s", ErrImagePathOutsideRoot, target)
	}
	return target, nil
}

// defaultImageWriteRoot: 优先当前 config.ImagePath（微信 Img 目录），否则可执行文件同级 image/。
func defaultImageWriteRoot() (string, error) {
	if root := strings.TrimSpace(config.ImagePath); root != "" {
		return filepath.Clean(root), nil
	}
	exe, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("%w: 无法定位可执行文件: %v", ErrImageWriteFailed, err)
	}
	return filepath.Join(filepath.Dir(exe), "image"), nil
}

// defaultCachedImagePathCheck 校验缓存里已有的 FilePath 能否直接复用：
// 必须在当前 config.ImagePath 范围内、是常规文件、大小合理、且内容真的是图片。
// 不满足就拒绝（绝不读任意路径）。
func defaultCachedImagePathCheck(path string) (bool, error) {
	root := strings.TrimSpace(config.ImagePath)
	if root == "" {
		return false, fmt.Errorf("%w: 未配置 image_path，无法校验缓存路径", ErrImagePathOutsideRoot)
	}
	if !withinRoot(root, path) {
		return false, fmt.Errorf("%w: 缓存路径越界", ErrImagePathOutsideRoot)
	}
	fi, err := os.Stat(path)
	if err != nil || !fi.Mode().IsRegular() || fi.Size() <= 0 {
		return false, fmt.Errorf("%w: 缓存路径不可用", ErrImagePathOutsideRoot)
	}
	if fi.Size() > maxRecoveredImageBytes {
		return false, fmt.Errorf("%w: cached_size=%d", ErrImageTooLarge, fi.Size())
	}
	return cachedImageFileCheck(path)
}

// cachedImageFileCheck 只读文件头部，确认内容真的是 PNG/JPEG/WEBP。
func cachedImageFileCheck(path string) (bool, error) {
	f, err := os.Open(path)
	if err != nil {
		return false, fmt.Errorf("%w: 打不开缓存文件", ErrImagePathOutsideRoot)
	}
	defer f.Close()
	head := make([]byte, imageMagicProbeBytes)
	n, _ := io.ReadFull(f, head)
	if _, err := sniffSupportedImage(head[:n]); err != nil {
		return false, fmt.Errorf("%w: 缓存文件头部不是 PNG/JPEG/WEBP", ErrImageNotImage)
	}
	return true, nil
}

// withinRoot 判断 p 是否严格位于 root 之内（解析符号链接后再比较）。
func withinRoot(root, p string) bool {
	if strings.TrimSpace(root) == "" || strings.TrimSpace(p) == "" {
		return false
	}
	absRoot, err1 := filepath.Abs(root)
	absPath, err2 := filepath.Abs(p)
	if err1 != nil || err2 != nil {
		return false
	}
	if resolved, err := filepath.EvalSymlinks(absRoot); err == nil {
		absRoot = resolved
	}
	if resolved, err := filepath.EvalSymlinks(absPath); err == nil {
		absPath = resolved
	}
	absRoot = filepath.Clean(absRoot)
	absPath = filepath.Clean(absPath)
	if absPath == absRoot {
		return false
	}
	return strings.HasPrefix(absPath, absRoot+string(filepath.Separator))
}

// safeCDNLabel 只回显 CDN 主机名：完整 URL 的 query 里可能带 aeskey，不能进日志。
func safeCDNLabel(raw string) string {
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return "invalid"
	}
	return u.Host
}

func magicHeadHex(data []byte) string {
	n := len(data)
	if n > 8 {
		n = 8
	}
	return hex.EncodeToString(data[:n])
}

// describeCandidates 生成不含 URL/key 的候选状态摘要，用于报错。
func describeCandidates(cands []imageCandidate) string {
	parts := make([]string, 0, len(cands))
	for _, c := range cands {
		state := "无缓存条目"
		if !c.hasKey {
			state = "缺少该候选自身的 AES key"
		}
		parts = append(parts, c.quality+"="+state)
	}
	return strings.Join(parts, ", ")
}
