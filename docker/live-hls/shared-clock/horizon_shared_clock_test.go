package hls

import (
	"net/http/httptest"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/bluenviron/gohlslib/v2"
	"github.com/bluenviron/gohlslib/v2/pkg/codecs"
	"github.com/bluenviron/mediacommon/v2/pkg/codecs/mpeg4audio"
	"github.com/bluenviron/mediacommon/v2/pkg/formats/fmp4"
	"github.com/bluenviron/mediamtx/internal/unit"
	"github.com/stretchr/testify/require"
)

func TestHorizonSharedClockRestartedRenditions(t *testing.T) {
	origin := time.Date(2026, 10, 31, 0, 0, 0, 0, time.UTC)
	var timestamps []int64
	callback := func(u *unit.Unit) error {
		timestamps = append(timestamps, u.PTS)
		return nil
	}
	for _, pts := range []int64{620 * 90000, 0} {
		mapper := horizonSharedClockCallback(&horizonSharedClock{origin: origin}, 90000, true, callback)
		u := &unit.Unit{
			PTS: pts, NTP: origin.Add(620 * time.Second),
			Payload: unit.PayloadH264([][]byte{{5}}),
		}
		require.NoError(t, mapper(u))
		require.Equal(t, pts, u.PTS)
	}
	require.Equal(t, []int64{620 * 90000, 620 * 90000}, timestamps)
}

func TestHorizonSharedClockPreservesPacingAndAudioClock(t *testing.T) {
	origin := time.Now()
	for _, rate := range []int{90000, 48000, 44100} {
		t.Run(strconv.Itoa(rate), func(t *testing.T) {
			var timestamps []int64
			mapper := horizonSharedClockCallback(&horizonSharedClock{origin: origin}, rate, true, func(u *unit.Unit) error {
				timestamps = append(timestamps, u.PTS)
				return nil
			})
			for i, arrival := range []time.Duration{10 * time.Second, 10500 * time.Millisecond} {
				u := &unit.Unit{
					PTS: int64(i) * int64(rate) / 25,
					NTP: origin.Add(arrival), Payload: unit.PayloadH264([][]byte{{5}}),
				}
				require.NoError(t, mapper(u))
			}
			require.Equal(t, []int64{int64(rate) * 10, int64(rate)*10 + int64(rate)/25}, timestamps)
		})
	}
}

func TestHorizonSharedClockRejectsMissingTiming(t *testing.T) {
	origin := time.Now()
	for _, candidate := range []struct {
		rate int
		ntp  time.Time
	}{
		{0, origin},
		{90000, time.Time{}},
		{90000, origin.Add(-time.Second)},
	} {
		called := false
		mapper := horizonSharedClockCallback(&horizonSharedClock{origin: origin}, candidate.rate, true, func(*unit.Unit) error {
			called = true
			return nil
		})
		require.Error(t, mapper(&unit.Unit{
			NTP: candidate.ntp, Payload: unit.PayloadH264([][]byte{{5}}),
		}))
		require.False(t, called)
	}
}

func TestHorizonSharedClockOptIn(t *testing.T) {
	for _, value := range []string{"", "0", "1", "invalid"} {
		t.Run(value, func(t *testing.T) {
			t.Setenv("HORIZON_HLS_SHARED_CLOCK", value)
			origin, err := horizonSharedClockOrigin()
			if value == "invalid" {
				require.Error(t, err)
			} else {
				require.NoError(t, err)
				require.Equal(t, value != "1", origin.IsZero())
				if value == "1" {
					require.Equal(t, time.Unix(0, 0), origin)
				}
			}
		})
	}
}

func TestHorizonSharedClockDefaultDoesNotChangeUnits(t *testing.T) {
	u := &unit.Unit{PTS: 123, Payload: unit.PayloadH264([][]byte{{5}})}
	mapper := horizonSharedClockCallback(&horizonSharedClock{}, 90000, true, func(mapped *unit.Unit) error {
		require.Same(t, u, mapped)
		return nil
	})
	require.NoError(t, mapper(u))
}

func TestHorizonSharedClockActualFMP4Clocks(t *testing.T) {
	origin := time.Unix(0, 0)
	wall := time.Date(2026, 10, 31, 0, 0, 0, 0, time.UTC)
	var clocks []uint64
	for _, sourceStart := range []int64{620, 0} {
		track := &gohlslib.Track{
			ClockRate: 90000,
			Codec: &codecs.H264{
				SPS: []byte{
					0x67, 0x42, 0xc0, 0x28, 0xd9, 0x00, 0x78, 0x02,
					0x27, 0xe5, 0x84, 0x00, 0x00, 0x03, 0x00, 0x04,
					0x00, 0x00, 0x03, 0x00, 0xf0, 0x3c, 0x60, 0xc9, 0x20,
				},
				PPS: []byte{8},
			},
		}
		muxer := &gohlslib.Muxer{
			Tracks:  []*gohlslib.Track{track},
			Variant: gohlslib.MuxerVariantFMP4, SegmentCount: 3,
			SegmentMinDuration: time.Second,
		}
		require.NoError(t, muxer.Start())
		t.Cleanup(muxer.Close)
		mapper := horizonSharedClockCallback(&horizonSharedClock{origin: origin}, 90000, true, func(u *unit.Unit) error {
			return muxer.WriteH264(track, u.NTP, u.PTS, u.Payload.(unit.PayloadH264))
		})
		for i := int64(0); i < 5; i++ {
			payload := unit.PayloadH264([][]byte{{5}})
			if i == 0 {
				codec := track.Codec.(*codecs.H264)
				payload = unit.PayloadH264([][]byte{codec.SPS, codec.PPS, {5}})
			}
			require.NoError(t, mapper(&unit.Unit{
				PTS:     (sourceStart + i) * 90000,
				NTP:     wall.Add(time.Duration(620+i) * time.Second),
				Payload: payload,
			}))
		}
		request := func(path string) []byte {
			response := httptest.NewRecorder()
			muxer.Handle(response, httptest.NewRequest("GET", "http://localhost/"+path, nil))
			require.Equal(t, 200, response.Code)
			return response.Body.Bytes()
		}
		var playlist, segment string
		for _, line := range strings.Split(string(request("index.m3u8")), "\n") {
			if line != "" && !strings.HasPrefix(line, "#") {
				playlist = line
				break
			}
		}
		require.NotEmpty(t, playlist)
		for _, line := range strings.Split(string(request(playlist)), "\n") {
			if line != "" && !strings.HasPrefix(line, "#") {
				segment = line
				break
			}
		}
		require.NotEmpty(t, segment)
		var parts fmp4.Parts
		require.NoError(t, parts.Unmarshal(request(segment)))
		require.NotEmpty(t, parts)
		require.NotEmpty(t, parts[0].Tracks)
		clocks = append(clocks, parts[0].Tracks[0].BaseTime)
	}
	require.GreaterOrEqual(t, clocks[0], uint64(wall.Unix()+630)*90000)
	require.Greater(t, clocks[0], uint64(1<<32))
	require.Equal(t, clocks[0], clocks[1])
}

func TestHorizonSharedClockKeepsOriginalAVTiming(t *testing.T) {
	wall := time.Date(2026, 10, 31, 0, 0, 0, 0, time.UTC)
	clock := &horizonSharedClock{origin: time.Unix(0, 0)}
	var videoPTS, audioPTS int64
	video := horizonSharedClockCallback(clock, 90000, true, func(u *unit.Unit) error {
		videoPTS = u.PTS
		return nil
	})
	audio := horizonSharedClockCallback(clock, 48000, false, func(u *unit.Unit) error {
		audioPTS = u.PTS
		return nil
	})
	audioUnit := &unit.Unit{
		PTS: 2*48000 + 960, NTP: wall.Add(500 * time.Millisecond),
		Payload: unit.PayloadH264([][]byte{{5}}),
	}
	require.NoError(t, audio(audioUnit))
	require.Zero(t, audioPTS)
	require.NoError(t, video(&unit.Unit{
		PTS: 2 * 90000, NTP: wall, Payload: unit.PayloadH264([][]byte{{5}}),
	}))
	require.NoError(t, audio(audioUnit))
	require.Equal(t, wall.Unix()*90000, videoPTS)
	require.Equal(t, wall.Unix()*48000+960, audioPTS)
	require.Equal(t, int64(2*48000+960), audioUnit.PTS)
}

func TestHorizonSharedClockEncodedAVTiming(t *testing.T) {
	wall := time.Date(2026, 10, 31, 0, 0, 0, 0, time.UTC)
	var offsets []float64
	for _, origin := range []time.Time{{}, time.Unix(0, 0)} {
		videoTrack := &gohlslib.Track{
			ClockRate: 90000,
			Codec: &codecs.H264{
				SPS: []byte{
					0x67, 0x42, 0xc0, 0x28, 0xd9, 0x00, 0x78, 0x02,
					0x27, 0xe5, 0x84, 0x00, 0x00, 0x03, 0x00, 0x04,
					0x00, 0x00, 0x03, 0x00, 0xf0, 0x3c, 0x60, 0xc9, 0x20,
				},
				PPS: []byte{8},
			},
		}
		audioTrack := &gohlslib.Track{
			ClockRate: 48000,
			Codec: &codecs.MPEG4Audio{Config: mpeg4audio.AudioSpecificConfig{
				Type: 2, SampleRate: 48000, ChannelConfig: 1,
			}},
		}
		muxer := &gohlslib.Muxer{
			Tracks:  []*gohlslib.Track{videoTrack, audioTrack},
			Variant: gohlslib.MuxerVariantFMP4, SegmentCount: 3,
			SegmentMinDuration: time.Second,
		}
		require.NoError(t, muxer.Start())
		t.Cleanup(muxer.Close)
		clock := &horizonSharedClock{origin: origin}
		video := horizonSharedClockCallback(clock, 90000, true, func(u *unit.Unit) error {
			return muxer.WriteH264(videoTrack, u.NTP, u.PTS, u.Payload.(unit.PayloadH264))
		})
		audio := horizonSharedClockCallback(clock, 48000, false, func(u *unit.Unit) error {
			return muxer.WriteMPEG4Audio(audioTrack, u.NTP, u.PTS, u.Payload.(unit.PayloadMPEG4Audio))
		})
		audioPTS := int64(2*48000 + 960)
		for i := int64(0); i < 100; i++ {
			videoPTS := 2*90000 + i*3600
			for audioPTS*90000 <= videoPTS*48000 {
				require.NoError(t, audio(&unit.Unit{
					PTS:     audioPTS,
					NTP:     wall.Add(time.Duration(audioPTS-2*48000)*time.Second/48000 + 500*time.Millisecond),
					Payload: unit.PayloadMPEG4Audio([][]byte{{1, 2, 3, 4}}),
				}))
				audioPTS += 1024
			}
			payload := unit.PayloadH264([][]byte{{1}})
			if i%25 == 0 {
				codec := videoTrack.Codec.(*codecs.H264)
				payload = unit.PayloadH264([][]byte{codec.SPS, codec.PPS, {5}})
			}
			require.NoError(t, video(&unit.Unit{
				PTS: videoPTS, NTP: wall.Add(time.Duration(i) * 40 * time.Millisecond),
				Payload: payload,
			}))
		}
		read := func(path string) []byte {
			response := httptest.NewRecorder()
			muxer.Handle(response, httptest.NewRequest("GET", "http://localhost/"+path, nil))
			require.Equal(t, 200, response.Code)
			return response.Body.Bytes()
		}
		master := string(read("index.m3u8"))
		audioURI := regexp.MustCompile(`#EXT-X-MEDIA:TYPE=AUDIO,[^\n]*URI="([^"]+)"`).FindStringSubmatch(master)
		require.Len(t, audioURI, 2)
		firstURI := func(text string) string {
			for _, line := range strings.Split(text, "\n") {
				if line != "" && !strings.HasPrefix(line, "#") {
					return line
				}
			}
			t.Fatal("Missing complete HLS object")
			return ""
		}
		startPTS := func(playlist string, rate float64) float64 {
			var parts fmp4.Parts
			require.NoError(t, parts.Unmarshal(read(firstURI(string(read(playlist))))))
			track := parts[0].Tracks[0]
			return (float64(track.BaseTime) + float64(track.Samples[0].PTSOffset)) / rate
		}
		offsets = append(offsets, startPTS(audioURI[1], 48000)-startPTS(firstURI(master), 90000))
	}
	require.InDelta(t, 0.02, offsets[0], 1.0/48000)
	require.InDelta(t, offsets[0], offsets[1], 1.0/48000)
}
