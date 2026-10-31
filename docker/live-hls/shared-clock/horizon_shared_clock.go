package hls

import (
	"fmt"
	"os"
	"time"

	"github.com/bluenviron/mediamtx/internal/stream"
	"github.com/bluenviron/mediamtx/internal/unit"
)

func horizonSharedClockOrigin() (time.Time, error) {
	switch os.Getenv("HORIZON_HLS_SHARED_CLOCK") {
	case "", "0":
		return time.Time{}, nil
	case "1":
		return time.Unix(0, 0), nil
	default:
		return time.Time{}, fmt.Errorf("HORIZON_HLS_SHARED_CLOCK must be 0 or 1")
	}
}

type horizonSharedClock struct {
	origin      time.Time
	initialized bool
	offset      time.Duration
}

func horizonSharedClockCallback(
	clock *horizonSharedClock,
	clockRate int,
	leading bool,
	onData stream.OnDataFunc,
) stream.OnDataFunc {
	if clock.origin.IsZero() {
		return onData
	}
	return func(u *unit.Unit) error {
		if u.NilPayload() {
			return onData(u)
		}
		// The muxer also discards audio until its leading video is available.
		if !clock.initialized && !leading {
			return nil
		}
		if clockRate <= 0 || u.NTP.IsZero() {
			return fmt.Errorf("shared HLS clock requires a clock rate and absolute timestamp")
		}
		if !clock.initialized {
			elapsed := u.NTP.Sub(clock.origin)
			if elapsed < 0 {
				return fmt.Errorf("shared HLS timestamp precedes the packager clock origin")
			}
			presentation := time.Duration(u.PTS/int64(clockRate))*time.Second +
				time.Duration(u.PTS%int64(clockRate))*time.Second/time.Duration(clockRate)
			clock.offset = elapsed - presentation
			clock.initialized = true
		}
		// One leading-track offset preserves the source's audio/video sync.
		// Reader callbacks are serialized, and the offset never follows arrival jitter.
		// Never mutate the shared unit used by other stream readers.
		offset := int64(clock.offset/time.Second)*int64(clockRate) +
			int64(clock.offset%time.Second)*int64(clockRate)/int64(time.Second)
		mapped := *u
		mapped.PTS += offset
		// Players align rungs with program dates as well as decode timestamps.
		// Arrival jitter must not give those two clocks different origins.
		presentation := time.Duration(mapped.PTS/int64(clockRate))*time.Second +
			time.Duration(mapped.PTS%int64(clockRate))*time.Second/time.Duration(clockRate)
		mapped.NTP = clock.origin.Add(presentation)
		return onData(&mapped)
	}
}
