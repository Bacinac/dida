export type NativeLocationIdentity = {
  provisioned: boolean;
  userId: string | null;
  origin: string | null;
};

export function nativeMatches(
  status: NativeLocationIdentity | null, userId: string | undefined, origin: string,
): boolean {
  return !!status?.provisioned && userId !== undefined && status.userId === userId && status.origin === origin;
}
