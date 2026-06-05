document.addEventListener("DOMContentLoaded", () => {
    let currentVersion = "0.1.0";
    async function loadVersion() {
        try {
            const res = await fetch("../../pyproject.toml");
            if (res.ok) {
                const text = await res.text();
                const match = text.match(/version\s*=\s*"([^"]+)"/);
                if (match?.[1]) {
                    currentVersion = match[1];
                }
            }
        } catch { }
        applyVersion();
    }

    function applyVersion() {
        document.querySelectorAll(".download-trigger").forEach(el => {
            const tier = el.dataset.tier;
            if (tier) {
                el.href = `https://pub-bd9548bbe1db4308b025de406732a5fa.r2.dev/vdev-ci/sage-${tier}-${currentVersion}-windows-x86_64.zip`;
            }
        });
        const badge = document.getElementById("version-badge");
        if (badge) {
            badge.textContent = `v${currentVersion} · Windows x86_64`;
        }
    }
    const toggleBtns = document.querySelectorAll(".toggle-btn");
    const tierCards = document.querySelectorAll(".tier-card");

    toggleBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            toggleBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");

            const filter = btn.dataset.filter;

            tierCards.forEach(card => {
                const type = card.dataset.tierType;
                const show = filter === "all" || type === filter;
                card.style.display = show ? "flex" : "none";

                if (show) {
                    card.style.opacity = "0";
                    requestAnimationFrame(() => {
                        card.style.transition = "opacity 0.25s ease";
                        card.style.opacity = "1";
                    });
                }
            });
        });
    });

    /* ─── Mobile menu ─── */
    const menuBtn = document.querySelector(".mobile-menu-btn");
    const headerNav = document.querySelector(".header-nav");

    if (menuBtn && headerNav) {
        menuBtn.addEventListener("click", () => {
            const open = headerNav.classList.toggle("mobile-open");
            const spans = menuBtn.querySelectorAll("span");

            if (open) {
                headerNav.style.display = "flex";
                spans[0].style.transform = "translateY(7px) rotate(45deg)";
                spans[1].style.opacity = "0";
                spans[2].style.transform = "translateY(-7px) rotate(-45deg)";
            } else {
                headerNav.style.display = "";
                spans[0].style.transform = "";
                spans[1].style.opacity = "";
                spans[2].style.transform = "";
            }
        });

        headerNav.querySelectorAll("a").forEach(link => {
            link.addEventListener("click", () => {
                if (headerNav.classList.contains("mobile-open")) {
                    menuBtn.click();
                }
            });
        });
    }
    loadVersion();
});
