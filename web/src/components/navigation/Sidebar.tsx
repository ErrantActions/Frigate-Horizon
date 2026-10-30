import Logo from "../Logo";
import NavItem from "./NavItem";
import { CameraGroupSelector } from "../filter/CameraGroupSelector";
import { Link, useMatch } from "react-router-dom";
import GeneralSettings from "../menu/GeneralSettings";
import AccountSettings from "../menu/AccountSettings";
import useNavigation from "@/hooks/use-navigation";
import { baseUrl } from "@/api/baseUrl";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import ToolsMenu from "./ToolsMenu";

function Sidebar() {
  const { t } = useTranslation("common");
  const basePath = useMemo(() => new URL(baseUrl).pathname, []);

  const isRootMatch = useMatch("/");
  const isBasePathMatch = useMatch(basePath);

  const navbarLinks = useNavigation();

  return (
    <aside className="scrollbar-container scrollbar-hidden absolute inset-y-0 left-0 z-10 flex w-[52px] flex-col justify-between overflow-y-auto border-r border-secondary-highlight bg-background_alt py-4">
      <span tabIndex={0} className="sr-only" />
      <div className="flex w-full flex-col items-center gap-0">
        <Link
          to="/"
          className="mb-4 flex size-11 shrink-0 items-center justify-center"
          aria-label={t("brand.home")}
          title={t("brand.name")}
        >
          <Logo className="size-8" />
        </Link>
        {navbarLinks
          .filter((item) => item.group !== "tools")
          .map((item) => {
            const showCameraGroups =
              (isRootMatch || isBasePathMatch) && item.id === 1;

            return (
              <div key={item.id}>
                <NavItem
                  large
                  className={`mx-1 size-11 shrink-0 ${showCameraGroups ? "mb-2" : "mb-3"}`}
                  item={item}
                  Icon={item.icon}
                />
                {showCameraGroups && <CameraGroupSelector className="mb-4" />}
              </div>
            );
          })}
        <ToolsMenu
          items={navbarLinks.filter((item) => item.group === "tools")}
          className="mx-1 mb-3 size-11"
          large
        />
      </div>
      <div className="mb-8 flex flex-col items-center gap-4">
        <GeneralSettings />
        <AccountSettings />
      </div>
    </aside>
  );
}

export default Sidebar;
