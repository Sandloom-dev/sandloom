/** Sandloom's woven mark; inherits the semantic text color. */
export function BrandMark({ className }: { className?: string }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 80 80" fill="none" className={className}>
      <path d="M18 12h32c12.15 0 22 9.85 22 22v12H58V34a8 8 0 0 0-8-8H18V12Z" fill="currentColor" />
      <path d="M62 68H30C17.85 68 8 58.15 8 46V34h14v12a8 8 0 0 0 8 8h32v14Z" fill="currentColor" />
      <path d="M30 33h20v14H30z" fill="currentColor" />
    </svg>
  );
}
