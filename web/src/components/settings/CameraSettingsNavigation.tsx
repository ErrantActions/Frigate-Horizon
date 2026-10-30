import { ReactNode, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuChevronRight, LuCopy } from "react-icons/lu";
import { CameraConfig } from "@/types/frigateConfig";
import {
  cameraSettingsGroups,
  isCameraSettingsView,
  SettingsType,
} from "@/types/settings";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Button } from "@/components/ui/button";
import { CameraNameLabel } from "@/components/camera/FriendlyNameLabel";
import { cn } from "@/lib/utils";

type CameraSettingsNavigationProps = {
  cameras: CameraConfig[];
  selectedCamera: string;
  page: SettingsType;
  pendingCameras: Set<string>;
  onSelectCamera: (camera: string) => void;
  onSelectView: (camera: string, view: SettingsType) => void;
  renderLabel: (view: SettingsType) => ReactNode;
  onManage: () => void;
  onCopy?: (camera: string) => void;
};

export default function CameraSettingsNavigation({
  cameras,
  selectedCamera,
  page,
  pendingCameras,
  onSelectCamera,
  onSelectView,
  renderLabel,
  onManage,
  onCopy,
}: CameraSettingsNavigationProps) {
  const { t } = useTranslation("views/settings");
  const [expandedCamera, setExpandedCamera] = useState<string>();
  const [expandedGroup, setExpandedGroup] = useState<string>();

  useEffect(() => {
    if (isCameraSettingsView(page) && selectedCamera) {
      setExpandedCamera(selectedCamera);
      setExpandedGroup(
        cameraSettingsGroups.find((group) =>
          group.views.some((view) => view === page),
        )?.label,
      );
    }
  }, [page, selectedCamera]);

  return (
    <div className="mb-3" data-camera-settings-navigation>
      <div className="px-2 py-2 text-sm font-medium text-secondary-foreground">
        {t("cameraNavigation.title")}
      </div>
      <Button
        variant="ghost"
        className="mb-1 h-auto min-h-9 w-full justify-start whitespace-normal px-2 text-left font-normal"
        onClick={onManage}
      >
        {t("menu.cameraManagement")}
      </Button>
      {cameras.map((camera) => {
        const expanded = expandedCamera === camera.name;
        const label = camera.friendly_name || camera.name;
        return (
          <Collapsible
            key={camera.name}
            open={expanded}
            onOpenChange={(open) => {
              setExpandedCamera(open ? camera.name : undefined);
              if (open) {
                onSelectCamera(camera.name);
              }
            }}
          >
            <div className="flex items-center gap-1">
              <CollapsibleTrigger asChild>
                <Button
                  variant="ghost"
                  aria-label={label}
                  className={cn(
                    "h-auto min-h-10 min-w-0 flex-1 justify-start gap-2 px-2 text-left",
                    selectedCamera === camera.name &&
                      isCameraSettingsView(page) &&
                      "text-primary",
                  )}
                >
                  <LuChevronRight
                    className={cn(
                      "size-4 shrink-0 transition-transform",
                      expanded && "rotate-90",
                    )}
                  />
                  <span className="min-w-0 flex-1 whitespace-normal break-words smart-capitalize">
                    <CameraNameLabel camera={camera.name} />
                  </span>
                  {!camera.enabled_in_config && (
                    <span className="text-xs text-muted-foreground">
                      {t("cameraNavigation.disabled")}
                    </span>
                  )}
                  {pendingCameras.has(camera.name) && (
                    <span
                      role="img"
                      aria-label={t("menuDot.unsaved")}
                      className="size-2 shrink-0 rounded-full bg-unsaved"
                    />
                  )}
                </Button>
              </CollapsibleTrigger>
              {onCopy && expanded && (
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={t("cameraNavigation.copyFrom", {
                    cameraName: label,
                  })}
                  onClick={() => onCopy(camera.name)}
                >
                  <LuCopy className="size-4" />
                </Button>
              )}
            </div>
            <CollapsibleContent className="ml-4 border-l border-secondary pl-2">
              {cameraSettingsGroups.map((group) => {
                const groupLabel =
                  group.label === "advancedSystem"
                    ? t("cameraNavigation.advanced")
                    : t("menu." + group.label);
                const hasActiveItem =
                  selectedCamera === camera.name &&
                  group.views.some((view) => view === page);
                return (
                  <Collapsible
                    key={group.label}
                    open={expandedGroup === group.label}
                    onOpenChange={(open) =>
                      setExpandedGroup(open ? group.label : undefined)
                    }
                    className="mb-1"
                  >
                    <CollapsibleTrigger asChild>
                      <Button
                        variant="ghost"
                        aria-label={t("cameraNavigation.openSection", {
                          cameraName: label,
                          section: groupLabel,
                        })}
                        className={cn(
                          "h-auto min-h-9 w-full justify-start gap-2 whitespace-normal px-2 py-1.5 text-left text-xs font-medium text-muted-foreground",
                          hasActiveItem && "text-primary",
                        )}
                      >
                        <LuChevronRight
                          className={cn(
                            "size-3 shrink-0 transition-transform",
                            expandedGroup === group.label && "rotate-90",
                          )}
                        />
                        {groupLabel}
                      </Button>
                    </CollapsibleTrigger>
                    <CollapsibleContent className="ml-2">
                      {group.views.map((view) => (
                        <Button
                          key={view}
                          variant="ghost"
                          aria-label={t("cameraNavigation.openSection", {
                            cameraName: label,
                            section: t("menu." + view),
                          })}
                          aria-current={
                            page === view && selectedCamera === camera.name
                              ? "page"
                              : undefined
                          }
                          className={cn(
                            "h-auto min-h-9 w-full justify-start whitespace-normal px-2 py-1.5 text-left font-normal",
                            page === view &&
                              selectedCamera === camera.name &&
                              "bg-selected/20 text-primary",
                          )}
                          onClick={() => onSelectView(camera.name, view)}
                        >
                          {renderLabel(view)}
                        </Button>
                      ))}
                    </CollapsibleContent>
                  </Collapsible>
                );
              })}
            </CollapsibleContent>
          </Collapsible>
        );
      })}
    </div>
  );
}
