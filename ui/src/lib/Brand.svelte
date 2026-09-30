<script lang="ts">
  import logo from "$lib/dida-logo.svg";

  // Without a size the logo takes its height from where it stands: the kit's frame
  // and about page size every product's mark alike. `md` is BABA's 168 matched by
  // HEIGHT (this logo is 3.41:1 against BABA's 3.80:1).
  let { size }: { size?: "md" } = $props();
</script>

<!-- An image, not a heading: the logo appears on five surfaces (sidebar, login,
     about, onboarding, wall panel) that carry headings of their own. -->
<img src={logo} alt="DIDA" class="block h-auto" style:width={size && "151px"} />
