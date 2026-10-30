import {
  createContext,
  useState,
  useEffect,
  ReactNode,
  useContext,
} from "react";
import { AllGroupsStreamingSettings } from "@/types/frigateConfig";
import { useUserPersistence } from "@/hooks/use-user-persistence";

type StreamingSettingsContextType = {
  allGroupsStreamingSettings: AllGroupsStreamingSettings;
  setAllGroupsStreamingSettings: (settings: AllGroupsStreamingSettings) => void;
  isPersistedStreamingSettingsLoaded: boolean;
};

const StreamingSettingsContext =
  createContext<StreamingSettingsContextType | null>(null);

export function StreamingSettingsProvider({
  children,
}: {
  children: ReactNode;
}) {
  const [allGroupsStreamingSettings, setAllGroupsStreamingSettings] =
    useState<AllGroupsStreamingSettings>({});

  const [
    persistedGroupStreamingSettings,
    setPersistedGroupStreamingSettings,
    isPersistedStreamingSettingsLoaded,
  ] = useUserPersistence<AllGroupsStreamingSettings>("streaming-settings");

  useEffect(() => {
    if (isPersistedStreamingSettingsLoaded) {
      const saved = persistedGroupStreamingSettings ?? {};
      const needsMigration = Object.values(saved).some((group) =>
        Object.values(group).some(
          (settings) =>
            "playerMode" in settings ||
            "compatibilityMode" in settings ||
            settings.dataSaver === undefined,
        ),
      );
      setAllGroupsStreamingSettings(
        needsMigration
          ? Object.fromEntries(
              Object.entries(saved).map(([group, settings]) => [
                group,
                Object.fromEntries(
                  Object.entries(settings).map(([camera, value]) => [
                    camera,
                    {
                      streamName: value.streamName,
                      streamType: value.streamType,
                      dataSaver:
                        value.dataSaver ??
                        ("playerMode" in value &&
                          value.playerMode === "jsmpeg"),
                      playAudio: value.playAudio,
                      volume: value.volume,
                    },
                  ]),
                ),
              ]),
            )
          : saved,
      );
    }
  }, [isPersistedStreamingSettingsLoaded, persistedGroupStreamingSettings]);

  useEffect(() => {
    if (Object.keys(allGroupsStreamingSettings).length) {
      setPersistedGroupStreamingSettings(allGroupsStreamingSettings);
    }
  }, [allGroupsStreamingSettings, setPersistedGroupStreamingSettings]);

  return (
    <StreamingSettingsContext
      value={{
        allGroupsStreamingSettings,
        setAllGroupsStreamingSettings,
        isPersistedStreamingSettingsLoaded,
      }}
    >
      {children}
    </StreamingSettingsContext>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useStreamingSettings() {
  const context = useContext(StreamingSettingsContext);
  if (!context) {
    throw new Error(
      "useStreamingSettings must be used within a StreamingSettingsProvider",
    );
  }
  return context;
}
