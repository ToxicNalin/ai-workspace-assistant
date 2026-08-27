/**
 * The free tier's most confusing behaviour, said out loud.
 *
 * Render spins the API down after ~15 minutes idle and Neon scales to zero
 * after 5, so the first request after a quiet period pays both and can take
 * the better part of a minute. SPEC-v2 §7 is explicit that this is documented
 * rather than hidden: a reviewer who reads this understands what they are
 * looking at, and one who does not concludes the app is broken.
 */
export function ColdStartNotice() {
  return (
    <p className="cold-start">
      <strong>First request may take up to a minute.</strong> The API and its database
      both sleep when idle on the free tier, and a cold request wakes both.
    </p>
  );
}
