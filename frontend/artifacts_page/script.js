document.addEventListener("DOMContentLoaded", () => {
    let currentVersion = "0.1.0";

    async function loadVersionAndInitialize() {
        try {
            const response = await fetch("../../pyproject.toml");
            if (response.ok) {
                const text = await response.text();
                const match = text.match(/version\s*=\s*"([^"]+)"/);
                if (match && match[1]) {
                    currentVersion = match[1];
                    console.log("Sage Hub: Loaded version dynamically from pyproject.toml ->", currentVersion);
                }
            }
        } catch (e) {
            console.warn("Sage Hub: CORS or direct file opening prevented fetching pyproject.toml. Using fallback version:", currentVersion);
        }

        applyDynamicVersions();
        updateAdvisory();
    }

    function applyDynamicVersions() {
        const downloadTriggers = document.querySelectorAll(".download-trigger");
        downloadTriggers.forEach(el => {
            const tier = el.getAttribute("data-tier");
            if (tier) {
                el.href = `https://pub-bd9548bbe1db4308b025de406732a5fa.r2.dev/vdev-ci/sage-${tier}-${currentVersion}-windows-x86_64.zip`;
            }
        });

        const shaBlock = document.getElementById("sha256-sums-content");
        if (shaBlock) {
            let text = shaBlock.textContent;
            text = text.replace(/0\.1\.0/g, currentVersion);
            shaBlock.textContent = text;
        }

        updateTerminalContent();
    }

    const calcGpu = document.getElementById("calc-gpu");
    const calcRam = document.getElementById("calc-ram");
    const calcConnection = document.getElementById("calc-connection");

    const recTierName = document.getElementById("recommended-tier-name");
    const recTierDesc = document.getElementById("recommended-tier-desc");
    const recSpeed = document.getElementById("rec-speed");
    const recSize = document.getElementById("rec-size");
    const recBackend = document.getElementById("rec-backend");
    const recCta = document.getElementById("rec-cta");

    function updateAdvisory() {
        if (!calcGpu || !calcRam || !calcConnection) return;

        const gpuVal = calcGpu.value;
        const ramVal = parseInt(calcRam.value);
        const connVal = calcConnection.value;

        let recommendation = {
            name: "Sage Fast (CPU Full)",
            desc: "Your CPU-only setup with 16GB+ RAM runs beautifully with our balanced AVX2 CPU Full tier. Packages local 2B and 0.8B models for private offline study assistance.",
            speed: "~12-18 t/s",
            size: "~3 GB",
            backend: "AVX2 CPU",
            targetId: "fast"
        };

        if (gpuVal === "nvidia-mid") {
            if (ramVal >= 16 && connVal === "fast") {
                recommendation = {
                    name: "Sage Pro (GPU Full)",
                    desc: "Your NVIDIA GPU card and 16GB+ RAM can host our flagship 4B model locally. Experience blazing fast generation with full CUDA acceleration.",
                    speed: "~35+ t/s",
                    size: "~5.0 GB",
                    backend: "CUDA 12.4",
                    targetId: "pro"
                };
            } else {
                recommendation = {
                    name: "Sage Pro-Lite (GPU Lite)",
                    desc: "Your GPU supports CUDA, but due to system constraints, we recommend the lightweight engine. Server binaries compile instantly; download and add models manually.",
                    speed: "~35+ t/s",
                    size: "~1.5 GB",
                    backend: "CUDA 12.4",
                    targetId: "pro-lite"
                };
            }
        } else {
            if (ramVal >= 16 && connVal === "fast") {
                recommendation = {
                    name: "Sage Fast (CPU Full)",
                    desc: "Your CPU-only setup with 16GB+ RAM runs beautifully with our balanced AVX2 CPU Full tier. Packages local 2B and 0.8B models.",
                    speed: "~12-18 t/s",
                    size: "~3 GB",
                    backend: "AVX2 CPU",
                    targetId: "fast"
                };
            } else {
                recommendation = {
                    name: "Sage Fast-Lite (CPU Lite)",
                    desc: "Perfect for initial fast installtion, download and add models manually for full experience.",
                    speed: "~8-12 t/s",
                    size: "~1.2 GB",
                    backend: "AVX2 CPU",
                    targetId: "fast-lite"
                };
            }
        }

        if (recTierName) recTierName.textContent = recommendation.name;
        if (recTierDesc) recTierDesc.textContent = recommendation.desc;
        if (recSpeed) recSpeed.textContent = recommendation.speed;
        if (recSize) recSize.textContent = recommendation.size;
        if (recBackend) recBackend.textContent = recommendation.backend;

        if (recCta) {
            recCta.setAttribute("href", "#tiers");
            recCta.onclick = (e) => {
                const cards = document.querySelectorAll(".tier-card");
                cards.forEach(card => card.classList.remove("featured-highlight"));

                setTimeout(() => {
                    const targetCard = document.querySelector(`.tier-card [data-tier="${recommendation.targetId}"]`)?.closest(".tier-card");
                    if (targetCard) {
                        targetCard.classList.add("featured-highlight");
                        setTimeout(() => {
                            targetCard.classList.remove("featured-highlight");
                        }, 3000);
                    }
                }, 500);
            };
        }
    }

    if (calcGpu && calcRam && calcConnection) {
        calcGpu.addEventListener("change", updateAdvisory);
        calcRam.addEventListener("change", updateAdvisory);
        calcConnection.addEventListener("change", updateAdvisory);
    }

    const filterChips = document.querySelectorAll(".filter-chip");
    const tierCards = document.querySelectorAll(".tier-card");

    filterChips.forEach(chip => {
        chip.addEventListener("click", () => {
            filterChips.forEach(c => c.classList.remove("active"));
            chip.classList.add("active");

            const filterVal = chip.getAttribute("data-filter");

            tierCards.forEach(card => {
                const types = card.getAttribute("data-tier-type").split(" ");
                if (filterVal === "all" || types.includes(filterVal)) {
                    card.style.display = "flex";
                    card.style.opacity = "0";
                    setTimeout(() => {
                        card.style.opacity = "1";
                        card.style.transition = "opacity 0.3s ease";
                    }, 50);
                } else {
                    card.style.display = "none";
                }
            });
        });
    });

    const terminalTierSelect = document.getElementById("terminal-tier-select");
    const terminalTabBtns = document.querySelectorAll(".terminal-tab-btn");
    const terminalPanes = document.querySelectorAll(".terminal-pane");
    const codePowershell = document.getElementById("code-powershell");
    const codeCmd = document.getElementById("code-cmd");

    let activeTerminalTab = "powershell";

    const powershellTemplates = {
        "pro": (v) => `$version = "${v}"
$tier = "pro"
$url = "https://pub-bd9548bbe1db4308b025de406732a5fa.r2.dev/vdev-ci/sage-$tier-$version-windows-x86_64.zip"
$outZip = "$env:TEMP\\sage-$tier.zip"
$outDir = "$env:TEMP\\sage-$tier-install"

Write-Host "Downloading Sage $tier ($url) to temporary files..." -ForegroundColor Magenta
Invoke-WebRequest -Uri $url -OutFile $outZip
Write-Host "Extracting archive payload..." -ForegroundColor Cyan
Expand-Archive -Path $outZip -DestinationPath $outDir -Force
Write-Host "Executing installer stub..." -ForegroundColor Green
Start-Process -FilePath "$outDir\\sage-$tier-$version-windows-x86_64.exe" -Wait
Write-Host "Cleaning up staging artifacts..." -ForegroundColor Yellow
Remove-Item $outZip, $outDir -Recurse -ErrorAction SilentlyContinue`,

        "fast": (v) => `$version = "${v}"
$tier = "fast"
$url = "https://pub-bd9548bbe1db4308b025de406732a5fa.r2.dev/vdev-ci/sage-$tier-$version-windows-x86_64.zip"
$outZip = "$env:TEMP\\sage-$tier.zip"
$outDir = "$env:TEMP\\sage-$tier-install"

Write-Host "Downloading Sage $tier ($url) to temporary files..." -ForegroundColor Magenta
Invoke-WebRequest -Uri $url -OutFile $outZip
Write-Host "Extracting archive payload..." -ForegroundColor Cyan
Expand-Archive -Path $outZip -DestinationPath $outDir -Force
Write-Host "Executing installer stub..." -ForegroundColor Green
Start-Process -FilePath "$outDir\\sage-$tier-$version-windows-x86_64.exe" -Wait
Write-Host "Cleaning up staging artifacts..." -ForegroundColor Yellow
Remove-Item $outZip, $outDir -Recurse -ErrorAction SilentlyContinue`,

        "pro-lite": (v) => `$version = "${v}"
$tier = "pro-lite"
$url = "https://pub-bd9548bbe1db4308b025de406732a5fa.r2.dev/vdev-ci/sage-$tier-$version-windows-x86_64.zip"
$outZip = "$env:TEMP\\sage-$tier.zip"
$outDir = "$env:TEMP\\sage-$tier-install"

Write-Host "Downloading Sage $tier ($url) to temporary files..." -ForegroundColor Magenta
Invoke-WebRequest -Uri $url -OutFile $outZip
Write-Host "Extracting archive payload..." -ForegroundColor Cyan
Expand-Archive -Path $outZip -DestinationPath $outDir -Force
Write-Host "Executing installer stub..." -ForegroundColor Green
Start-Process -FilePath "$outDir\\sage-$tier-$version-windows-x86_64.exe" -Wait
Write-Host "Cleaning up staging artifacts..." -ForegroundColor Yellow
Remove-Item $outZip, $outDir -Recurse -ErrorAction SilentlyContinue`,

        "fast-lite": (v) => `$version = "${v}"
$tier = "fast-lite"
$url = "https://pub-bd9548bbe1db4308b025de406732a5fa.r2.dev/vdev-ci/sage-$tier-$version-windows-x86_64.zip"
$outZip = "$env:TEMP\\sage-$tier.zip"
$outDir = "$env:TEMP\\sage-$tier-install"

Write-Host "Downloading Sage $tier ($url) to temporary files..." -ForegroundColor Magenta
Invoke-WebRequest -Uri $url -OutFile $outZip
Write-Host "Extracting archive payload..." -ForegroundColor Cyan
Expand-Archive -Path $outZip -DestinationPath $outDir -Force
Write-Host "Executing installer stub..." -ForegroundColor Green
Start-Process -FilePath "$outDir\\sage-$tier-$version-windows-x86_64.exe" -Wait
Write-Host "Cleaning up staging artifacts..." -ForegroundColor Yellow
Remove-Item $outZip, $outDir -Recurse -ErrorAction SilentlyContinue`
    };

    const cmdTemplates = {
        "pro": (v) => `curl -L -o sage-pro-bundle.zip https://pub-bd9548bbe1db4308b025de406732a5fa.r2.dev/vdev-ci/sage-pro-${v}-windows-x86_64.zip
tar -xf sage-pro-bundle.zip
start sage-pro-${v}-windows-x86_64.exe`,

        "fast": (v) => `curl -L -o sage-fast-bundle.zip https://pub-bd9548bbe1db4308b025de406732a5fa.r2.dev/vdev-ci/sage-fast-${v}-windows-x86_64.zip
tar -xf sage-fast-bundle.zip
start sage-fast-${v}-windows-x86_64.exe`,

        "pro-lite": (v) => `curl -L -o sage-pro-lite-bundle.zip https://pub-bd9548bbe1db4308b025de406732a5fa.r2.dev/vdev-ci/sage-pro-lite-${v}-windows-x86_64.zip
tar -xf sage-pro-lite-bundle.zip
start sage-pro-lite-${v}-windows-x86_64.exe`,

        "fast-lite": (v) => `curl -L -o sage-fast-lite-bundle.zip https://pub-bd9548bbe1db4308b025de406732a5fa.r2.dev/vdev-ci/sage-fast-lite-${v}-windows-x86_64.zip
tar -xf sage-fast-lite-bundle.zip
start sage-fast-lite-${v}-windows-x86_64.exe`
    };

    function updateTerminalContent() {
        if (!terminalTierSelect || !codePowershell || !codeCmd) return;
        const tier = terminalTierSelect.value;
        codePowershell.textContent = powershellTemplates[tier](currentVersion);
        codeCmd.textContent = cmdTemplates[tier](currentVersion);
    }

    terminalTabBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            terminalTabBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");

            const tabId = btn.getAttribute("data-terminal-tab");
            activeTerminalTab = tabId;

            terminalPanes.forEach(pane => pane.classList.remove("active"));
            const targetPane = document.getElementById(`pane-${tabId}`);
            if (targetPane) targetPane.classList.add("active");

            const copyBtn = document.getElementById("terminal-copy-btn");
            if (copyBtn) {
                if (tabId === "manual") {
                    copyBtn.style.display = "none";
                } else {
                    copyBtn.style.display = "inline-flex";
                }
            }
        });
    });

    if (terminalTierSelect) {
        terminalTierSelect.addEventListener("change", updateTerminalContent);
    }

    const terminalCopyBtn = document.getElementById("terminal-copy-btn");
    if (terminalCopyBtn) {
        terminalCopyBtn.addEventListener("click", () => {
            let codeText = "";
            if (activeTerminalTab === "powershell") {
                codeText = codePowershell.textContent;
            } else if (activeTerminalTab === "cmd") {
                codeText = codeCmd.textContent;
            }

            navigator.clipboard.writeText(codeText).then(() => {
                const btnSpan = terminalCopyBtn.querySelector("span");
                const originalText = btnSpan.textContent;

                btnSpan.textContent = "Copied!";
                terminalCopyBtn.style.borderColor = "var(--color-accent-emerald)";
                terminalCopyBtn.style.background = "rgba(16, 182, 129, 0.1)";

                setTimeout(() => {
                    btnSpan.textContent = originalText;
                    terminalCopyBtn.style.borderColor = "";
                    terminalCopyBtn.style.background = "";
                }, 2000);
            }).catch(err => {
                console.error("Clipboard copy failed", err);
            });
        });
    }

    const copyChecksumsBtn = document.getElementById("copy-checksums-btn");
    const shaSumsContent = document.getElementById("sha256-sums-content");
    if (copyChecksumsBtn && shaSumsContent) {
        copyChecksumsBtn.addEventListener("click", () => {
            navigator.clipboard.writeText(shaSumsContent.textContent).then(() => {
                const originalText = copyChecksumsBtn.textContent;
                copyChecksumsBtn.textContent = "Checksum Signatures Copied!";
                copyChecksumsBtn.style.background = "rgba(16, 182, 129, 0.15)";
                copyChecksumsBtn.style.color = "var(--color-accent-emerald)";
                copyChecksumsBtn.style.borderColor = "rgba(16, 182, 129, 0.3)";

                setTimeout(() => {
                    copyChecksumsBtn.textContent = originalText;
                    copyChecksumsBtn.style.background = "";
                    copyChecksumsBtn.style.color = "";
                    copyChecksumsBtn.style.borderColor = "";
                }, 2000);
            });
        });
    }

    const faqItems = document.querySelectorAll(".faq-item");
    faqItems.forEach(item => {
        item.addEventListener("click", () => {
            const isActive = item.classList.contains("active");
            faqItems.forEach(i => i.classList.remove("active"));
            if (!isActive) {
                item.classList.add("active");
            }
        });
    });

    const mobileMenuBtn = document.querySelector(".mobile-menu-btn");
    const navLinks = document.querySelector(".nav-links");

    if (mobileMenuBtn && navLinks) {
        mobileMenuBtn.addEventListener("click", () => {
            const isActive = navLinks.classList.contains("mobile-active");

            if (isActive) {
                navLinks.classList.remove("mobile-active");
                navLinks.style.display = "none";
                mobileMenuBtn.querySelectorAll("span")[0].style.transform = "none";
                mobileMenuBtn.querySelectorAll("span")[1].style.opacity = "1";
                mobileMenuBtn.querySelectorAll("span")[2].style.transform = "none";
            } else {
                navLinks.classList.add("mobile-active");
                navLinks.style.display = "flex";
                navLinks.style.flexDirection = "column";
                navLinks.style.position = "absolute";
                navLinks.style.top = "var(--header-height)";
                navLinks.style.left = "0";
                navLinks.style.width = "100%";
                navLinks.style.background = "rgba(3, 7, 18, 0.95)";
                navLinks.style.backdropFilter = "blur(20px)";
                navLinks.style.padding = "24px";
                navLinks.style.borderBottom = "1px solid var(--color-panel-border)";
                navLinks.style.gap = "20px";

                mobileMenuBtn.querySelectorAll("span")[0].style.transform = "translateY(8px) rotate(45deg)";
                mobileMenuBtn.querySelectorAll("span")[1].style.opacity = "0";
                mobileMenuBtn.querySelectorAll("span")[2].style.transform = "translateY(-8px) rotate(-45deg)";
            }
        });

        navLinks.querySelectorAll("a").forEach(link => {
            link.addEventListener("click", () => {
                if (navLinks.classList.contains("mobile-active")) {
                    mobileMenuBtn.click();
                }
            });
        });
    }

    loadVersionAndInitialize();
});
