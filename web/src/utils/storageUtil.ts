export const getUnitSize = (MB: number) => {
  if (!Number.isFinite(MB) || MB === null || MB < 0) return "-";
  if (MB < 1024) return `${MB.toFixed(2)} MiB`;
  if (MB < 1048576) return `${(MB / 1024).toFixed(2)} GiB`;

  return `${(MB / 1048576).toFixed(2)} TiB`;
};

export const getUnitSizeFromBytes = (bytes: number) => {
  if (!Number.isFinite(bytes) || bytes === null || bytes < 0) return "-";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(2)} KiB`;

  return getUnitSize(bytes / 1048576);
};
