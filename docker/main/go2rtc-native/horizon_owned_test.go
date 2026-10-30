// SPDX-License-Identifier: MIT
package webrtc

import (
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestHorizonOwnedExpiryClosesOnlyItsPeer(t *testing.T) {
	now := time.Now()
	registry := newHorizonRegistry(func() time.Time { return now }, 2)
	first, _ := registry.reserve()
	second, _ := registry.reserve()
	var a, b atomic.Int32
	registry.attach(first, func() { a.Add(1) })
	registry.attach(second, func() { b.Add(1) })
	peer := registry.peers[first]
	now = now.Add(16 * time.Second)
	registry.expire(first, peer)
	if a.Load() != 1 || b.Load() != 0 {
		t.Fatal("expiry affected another peer")
	}
	registry.drop(second)
}

func TestHorizonOwnedLateAttachImmediatelyClosesUnclaimedPeer(t *testing.T) {
	now := time.Now()
	registry := newHorizonRegistry(func() time.Time { return now }, 1)
	id, _ := registry.reserve()
	registry.drop(id)
	closed := false
	if registry.attach(id, func() { closed = true }) || !closed {
		t.Fatal("late creation revived an expired peer")
	}
}

func TestHorizonOwnedRenewHasHardLimitAndCannotReviveExpiry(t *testing.T) {
	now := time.Now()
	registry := newHorizonRegistry(func() time.Time { return now }, 1)
	id, _ := registry.reserve()
	closed := false
	registry.attach(id, func() { closed = true })
	for index := 0; index < 23; index++ {
		now = now.Add(5 * time.Second)
		if _, ok := registry.renew(id); !ok {
			t.Fatal("valid renewal failed")
		}
	}
	now = now.Add(6 * time.Second)
	if _, ok := registry.renew(id); ok || !closed {
		t.Fatal("hard limit was extended")
	}
	if _, ok := registry.renew(id); ok {
		t.Fatal("closed peer was revived")
	}
}

func TestHorizonOwnedStaleTimerDoesNotCloseRenewedPeer(t *testing.T) {
	now := time.Now()
	registry := newHorizonRegistry(func() time.Time { return now }, 1)
	id, _ := registry.reserve()
	var closed bool
	registry.attach(id, func() { closed = true })
	peer := registry.peers[id]
	now = now.Add(10 * time.Second)
	registry.renew(id)
	now = now.Add(6 * time.Second)
	registry.expire(id, peer)
	if closed {
		t.Fatal("stale timer closed a renewed peer")
	}
	registry.drop(id)
}

func TestHorizonOwnedCapacityFailsClosedAndDeleteIsIdempotent(t *testing.T) {
	registry := newHorizonRegistry(time.Now, 1)
	id, _ := registry.reserve()
	if _, err := registry.reserve(); err == nil {
		t.Fatal("capacity bound was waived")
	}
	registry.drop(id)
	registry.drop(id)
	next, err := registry.reserve()
	if err != nil {
		t.Fatal("closed capacity was not released")
	}
	registry.drop(next)
}

func TestHorizonOwnedPrivateFenceRejectsProxyAndNonLoopback(t *testing.T) {
	r := httptest.NewRequest("GET", "http://localhost/api/horizon/webrtc", nil)
	r.RemoteAddr = "127.0.0.1:12345"
	if !horizonPrivateRequest(r) {
		t.Fatal("local control was rejected")
	}
	r.Header.Set("X-Forwarded-For", "192.0.2.1")
	if horizonPrivateRequest(r) {
		t.Fatal("proxied control was accepted")
	}
	r.Header.Del("X-Forwarded-For")
	r.RemoteAddr = "192.0.2.1:12345"
	if horizonPrivateRequest(r) {
		t.Fatal("remote control was accepted")
	}
}

func TestHorizonOwnedRequiresOnlyEncryptedAudio(t *testing.T) {
	audio := "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=fingerprint:sha-256 TEST\r\n"
	if !horizonAudioOffer(audio) {
		t.Fatal("audio offer rejected")
	}
	for _, invalid := range []string{
		strings.Replace(audio, "m=audio", "m=video", 1),
		audio + "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n",
		strings.Replace(audio, "UDP/TLS/RTP/SAVPF", "RTP/AVP", 1),
		strings.Replace(audio, "a=fingerprint:", "a=invalid:", 1),
	} {
		if horizonAudioOffer(invalid) {
			t.Fatal("unsupported media offer accepted")
		}
	}
}
