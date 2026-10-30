// SPDX-License-Identifier: MIT
package webrtc

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/AlexxIT/go2rtc/internal/api"
	"github.com/AlexxIT/go2rtc/internal/streams"
	"github.com/AlexxIT/go2rtc/pkg/core"
	rtc "github.com/AlexxIT/go2rtc/pkg/webrtc"
	pion "github.com/pion/webrtc/v4"
)

const horizonTTL = 15 * time.Second
const horizonMaximum = 120 * time.Second

type horizonPeer struct {
	deadline time.Time
	maximum  time.Time
	close    func()
	timer    *time.Timer
}

type horizonRegistry struct {
	mu       sync.Mutex
	peers    map[string]*horizonPeer
	now      func() time.Time
	capacity int
}

func newHorizonRegistry(now func() time.Time, capacity int) *horizonRegistry {
	return &horizonRegistry{peers: make(map[string]*horizonPeer), now: now, capacity: capacity}
}

func (r *horizonRegistry) reserve() (string, error) {
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	id := hex.EncodeToString(raw)
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.peers) >= r.capacity {
		return "", errors.New("owned media capacity reached")
	}
	now := r.now()
	peer := &horizonPeer{deadline: now.Add(horizonTTL), maximum: now.Add(horizonMaximum)}
	r.peers[id] = peer
	peer.timer = time.AfterFunc(horizonTTL, func() { r.expire(id, peer) })
	return id, nil
}

func (r *horizonRegistry) attach(id string, close func()) bool {
	r.mu.Lock()
	peer := r.peers[id]
	accepted := peer != nil && r.now().Before(peer.deadline)
	if accepted {
		peer.close = close
	}
	r.mu.Unlock()
	if !accepted {
		close()
	}
	return accepted
}

func (r *horizonRegistry) drop(id string) {
	r.mu.Lock()
	peer := r.peers[id]
	delete(r.peers, id)
	if peer != nil && peer.timer != nil {
		peer.timer.Stop()
	}
	r.mu.Unlock()
	if peer != nil && peer.close != nil {
		peer.close()
	}
}

func (r *horizonRegistry) detached(id string) {
	r.mu.Lock()
	peer := r.peers[id]
	delete(r.peers, id)
	if peer != nil && peer.timer != nil {
		peer.timer.Stop()
	}
	r.mu.Unlock()
}

func (r *horizonRegistry) expire(id string, expected *horizonPeer) {
	r.mu.Lock()
	current := r.peers[id]
	if current != expected {
		r.mu.Unlock()
		return
	}
	delay := current.deadline.Sub(r.now())
	if delay > 0 {
		current.timer = time.AfterFunc(delay, func() { r.expire(id, expected) })
		r.mu.Unlock()
		return
	}
	delete(r.peers, id)
	r.mu.Unlock()
	if current.close != nil {
		current.close()
	}
}

func (r *horizonRegistry) renew(id string) (time.Time, bool) {
	r.mu.Lock()
	peer := r.peers[id]
	now := r.now()
	if peer == nil || !now.Before(peer.deadline) || !now.Before(peer.maximum) {
		r.mu.Unlock()
		r.drop(id)
		return time.Time{}, false
	}
	peer.deadline = now.Add(horizonTTL)
	if peer.maximum.Before(peer.deadline) {
		peer.deadline = peer.maximum
	}
	deadline := peer.deadline
	if peer.timer != nil {
		peer.timer.Stop()
	}
	peer.timer = time.AfterFunc(deadline.Sub(now), func() { r.expire(id, peer) })
	r.mu.Unlock()
	return deadline, true
}

var horizonOwned = newHorizonRegistry(time.Now, 64)

func initHorizonOwned() {
	api.HandleFunc("api/horizon/webrtc", horizonOwnedHandler)
}

func horizonPrivateRequest(r *http.Request) bool {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() {
		return false
	}
	for _, name := range []string{"Origin", "Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP"} {
		if r.Header.Get(name) != "" {
			return false
		}
	}
	return true
}

func horizonJSON(w http.ResponseWriter, status int, value any) error {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "private, no-store")
	w.WriteHeader(status)
	return json.NewEncoder(w).Encode(value)
}

func horizonOwnedHandler(w http.ResponseWriter, r *http.Request) {
	if !horizonPrivateRequest(r) {
		http.Error(w, "Private media control only", http.StatusForbidden)
		return
	}
	id := r.URL.Query().Get("id")
	switch r.Method {
	case http.MethodGet:
		_ = horizonJSON(w, http.StatusOK, map[string]any{
			"protocol": "horizon-owned-webrtc-v1", "owned_consumers": true,
			"lease_ttl_seconds": 15, "max_duration_seconds": 120,
		})
	case http.MethodPatch:
		if len(id) != 32 {
			http.Error(w, "Owned media not found", http.StatusNotFound)
			return
		}
		if deadline, ok := horizonOwned.renew(id); ok {
			_ = horizonJSON(w, http.StatusOK, map[string]any{
				"peer_id": id, "expires_at": float64(deadline.UnixNano()) / 1e9, "renew_after_ms": 5000,
			})
		} else {
			http.Error(w, "Owned media expired", http.StatusNotFound)
		}
	case http.MethodDelete:
		if len(id) != 32 {
			http.Error(w, "Owned media not found", http.StatusNotFound)
			return
		}
		horizonOwned.drop(id)
		_ = horizonJSON(w, http.StatusOK, map[string]bool{"success": true})
	case http.MethodPost:
		horizonCreate(w, r)
	default:
		http.Error(w, "Unsupported media control method", http.StatusMethodNotAllowed)
	}
}

func horizonAudioOffer(sdp string) bool {
	count := 0
	fingerprint := false
	if !strings.HasPrefix(sdp, "v=0") || strings.ContainsRune(sdp, '\x00') {
		return false
	}
	for _, line := range strings.Split(strings.ReplaceAll(sdp, "\r\n", "\n"), "\n") {
		if strings.HasPrefix(line, "m=") {
			parts := strings.Fields(line)
			if len(parts) < 4 || parts[0] != "m=audio" || parts[1] == "0" ||
				(parts[2] != "UDP/TLS/RTP/SAVPF" && parts[2] != "TCP/TLS/RTP/SAVPF") {
				return false
			}
			count++
		}
		if strings.HasPrefix(line, "a=fingerprint:") {
			fingerprint = true
		}
	}
	return count == 1 && fingerprint
}

func horizonCreate(w http.ResponseWriter, r *http.Request) {
	stream := streams.Get(r.URL.Query().Get("src"))
	if stream == nil {
		http.Error(w, "Configured media source unavailable", http.StatusNotFound)
		return
	}
	var offer struct {
		Type string `json:"type"`
		SDP  string `json:"sdp"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 131072))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&offer) != nil || offer.Type != "offer" || !horizonAudioOffer(offer.SDP) {
		http.Error(w, "Encrypted audio offer required", http.StatusBadRequest)
		return
	}
	id, err := horizonOwned.reserve()
	if err != nil {
		http.Error(w, "Owned media unavailable", http.StatusServiceUnavailable)
		return
	}
	success := false
	defer func() {
		if !success {
			horizonOwned.drop(id)
		}
	}()
	stopCancel := context.AfterFunc(r.Context(), func() { horizonOwned.drop(id) })
	defer stopCancel()
	pc, err := PeerConnection(false)
	if err != nil {
		http.Error(w, "Audio negotiation unavailable", http.StatusServiceUnavailable)
		return
	}
	conn := rtc.NewConn(pc)
	conn.Mode = core.ModePassiveConsumer
	conn.Protocol = "horizon-owned"
	conn.UserAgent = "HorizonNativeTalkback/1"
	conn.Listen(func(value any) {
		if state, ok := value.(pion.PeerConnectionState); ok && state == pion.PeerConnectionStateClosed {
			horizonOwned.detached(id)
			stream.RemoveConsumer(conn)
		}
	})
	if !horizonOwned.attach(id, func() { _ = conn.Close() }) {
		http.Error(w, "Audio negotiation expired", http.StatusServiceUnavailable)
		return
	}
	if conn.SetOffer(offer.SDP) != nil || stream.AddConsumer(conn) != nil {
		http.Error(w, "Audio backchannel unavailable", http.StatusServiceUnavailable)
		return
	}
	answer, err := conn.GetCompleteAnswer(GetCandidates(), FilterCandidate)
	if err != nil || r.Context().Err() != nil {
		http.Error(w, "Audio negotiation failed", http.StatusServiceUnavailable)
		return
	}
	deadline, ok := horizonOwned.renew(id)
	if !ok {
		http.Error(w, "Audio negotiation expired", http.StatusServiceUnavailable)
		return
	}
	if horizonJSON(w, http.StatusOK, map[string]any{
		"type": "answer", "sdp": answer, "peer_id": id,
		"expires_at": float64(deadline.UnixNano()) / 1e9, "renew_after_ms": 5000,
	}) != nil {
		return
	}
	success = true
}
