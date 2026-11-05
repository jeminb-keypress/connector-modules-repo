/** @odoo-module **/

import { loadBundle } from "@web/core/assets";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import {
    Component,
    onMounted,
    onPatched,
    onWillStart,
    onWillUnmount,
    proxy,
    signal,
} from "@odoo/owl";

const DATE_OPTIONS = [
    { value: "today", label: _t("Today") },
    { value: "last_7", label: _t("Last 7 Days") },
    { value: "last_30", label: _t("Last 30 Days") },
    { value: "last_90", label: _t("Last 90 Days") },
    { value: "custom", label: _t("Custom Date Range") },
];

export class WooDashboard extends Component {
    static template = "kits_odoo_woocommerce_connector.WooDashboard";

    overviewCanvas = signal.ref();
    statusCanvas = signal.ref();
    state = proxy({
        loading: true,
        error: false,
        dateRange: "last_30",
        customFrom: "",
        customTo: "",
        data: {
            kpis: {},
            sync_overview: { labels: [], products: [], customers: [], orders: [] },
            stores: [],
            sync_status: { done: 0, failed: 0, pending: 0, success_rate: 0 },
            top_products: [],
            pending_jobs: [],
        },
    });

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.dateOptions = DATE_OPTIONS;
        this._overviewChart = null;
        this._statusChart = null;

        onWillStart(async () => {
            await loadBundle("web.chartjs_lib");
            await this.loadData();
        });
        onMounted(() => this.renderCharts());
        onPatched(() => this.renderCharts());
        onWillUnmount(() => this.destroyCharts());
    }

    get kpis() {
        return this.state.data.kpis || {};
    }

    get stores() {
        return this.state.data.stores || [];
    }

    get topProducts() {
        return this.state.data.top_products || [];
    }

    get pendingJobs() {
        return this.state.data.pending_jobs || [];
    }

    get syncStatus() {
        return this.state.data.sync_status || {};
    }

    get showCustomDates() {
        return this.state.dateRange === "custom";
    }

    async loadData() {
        this.state.loading = true;
        this.state.error = false;
        try {
            const payload = await this.orm.call(
                "kits.connector.instance",
                "get_woo_dashboard_data",
                [
                    this.state.dateRange,
                    this.state.customFrom || false,
                    this.state.customTo || false,
                ]
            );
            this.state.data = payload;
        } catch (error) {
            this.state.error = error?.data?.message || error?.message || _t("Failed to load dashboard.");
            this.notification.add(this.state.error, { type: "danger" });
        } finally {
            this.state.loading = false;
        }
    }

    async onDateRangeChange(ev) {
        this.state.dateRange = ev.target.value;
        if (this.state.dateRange !== "custom") {
            await this.loadData();
        }
    }

    async onApplyCustom() {
        if (!this.state.customFrom || !this.state.customTo) {
            this.notification.add(_t("Choose both From and To dates."), { type: "warning" });
            return;
        }
        await this.loadData();
    }

    onCustomFromChange(ev) {
        this.state.customFrom = ev.target.value;
    }

    onCustomToChange(ev) {
        this.state.customTo = ev.target.value;
    }

    destroyCharts() {
        if (this._overviewChart) {
            this._overviewChart.destroy();
            this._overviewChart = null;
        }
        if (this._statusChart) {
            this._statusChart.destroy();
            this._statusChart = null;
        }
    }

    renderCharts() {
        if (this.state.loading || typeof Chart === "undefined") {
            return;
        }
        this.renderOverviewChart();
        this.renderStatusChart();
    }

    renderOverviewChart() {
        const canvas = this.overviewCanvas();
        if (!canvas) {
            return;
        }
        const overview = this.state.data.sync_overview || {};
        const labels = overview.labels || [];
        if (this._overviewChart) {
            this._overviewChart.destroy();
        }
        this._overviewChart = new Chart(canvas, {
            type: "line",
            data: {
                labels,
                datasets: [
                    {
                        label: _t("Products"),
                        data: overview.products || [],
                        borderColor: "#5B8DEF",
                        backgroundColor: "rgba(91, 141, 239, 0.18)",
                        fill: true,
                        tension: 0.35,
                        pointRadius: 0,
                    },
                    {
                        label: _t("Customers"),
                        data: overview.customers || [],
                        borderColor: "#7EB6FF",
                        backgroundColor: "rgba(126, 182, 255, 0.12)",
                        fill: true,
                        tension: 0.35,
                        pointRadius: 0,
                    },
                    {
                        label: _t("Orders"),
                        data: overview.orders || [],
                        borderColor: "#F0A05A",
                        backgroundColor: "rgba(240, 160, 90, 0.12)",
                        fill: true,
                        tension: 0.35,
                        pointRadius: 0,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                resizeDelay: 100,
                interaction: { mode: "index", intersect: false },
                plugins: {
                    legend: {
                        position: "bottom",
                        labels: {
                            boxWidth: 10,
                            boxHeight: 10,
                            font: { size: window.matchMedia("(max-width: 575.98px)").matches ? 10 : 12 },
                        },
                    },
                    tooltip: { enabled: true },
                },
                scales: {
                    x: {
                        ticks: {
                            maxRotation: window.matchMedia("(max-width: 575.98px)").matches ? 45 : 0,
                            autoSkip: true,
                            maxTicksLimit: window.matchMedia("(max-width: 575.98px)").matches ? 5 : 8,
                            callback(value) {
                                const label = this.getLabelForValue(value);
                                return label ? String(label).slice(5) : "";
                            },
                        },
                        grid: { display: false },
                    },
                    y: {
                        beginAtZero: true,
                        ticks: { precision: 0 },
                        title: {
                            display: !window.matchMedia("(max-width: 575.98px)").matches,
                            text: _t("Jobs completed"),
                        },
                    },
                },
            },
        });
    }

    renderStatusChart() {
        const canvas = this.statusCanvas();
        if (!canvas) {
            return;
        }
        const status = this.syncStatus;
        if (this._statusChart) {
            this._statusChart.destroy();
        }
        this._statusChart = new Chart(canvas, {
            type: "doughnut",
            data: {
                labels: [_t("Successful"), _t("Failed"), _t("Pending")],
                datasets: [
                    {
                        data: [status.done || 0, status.failed || 0, status.pending || 0],
                        backgroundColor: ["#3DDC97", "#F06565", "#F0B429"],
                        borderWidth: 0,
                        hoverOffset: 4,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: "72%",
                plugins: {
                    legend: { display: false },
                    tooltip: { enabled: true },
                },
            },
        });
    }

    kpiValue(key) {
        return this.kpis[key]?.value ?? 0;
    }

    kpiChange(key) {
        return this.kpis[key]?.change_label || false;
    }

    kpiChangeClass(key) {
        const positive = this.kpis[key]?.change_positive;
        if (positive === true) {
            return "text-success";
        }
        if (positive === false) {
            return "text-danger";
        }
        return "text-muted";
    }

    formatRate(value) {
        const num = Number(value || 0);
        return `${num.toFixed(1)}%`;
    }

    storeStateClass(state) {
        if (state === "connected") {
            return "o_woo_badge o_woo_badge_success";
        }
        if (state === "error") {
            return "o_woo_badge o_woo_badge_danger";
        }
        return "o_woo_badge o_woo_badge_muted";
    }

    jobStateClass(state) {
        if (state === "pending") {
            return "o_woo_badge o_woo_badge_warning";
        }
        if (state === "running") {
            return "o_woo_badge o_woo_badge_info";
        }
        return "o_woo_badge o_woo_badge_muted";
    }

    async openXmlAction(xmlId) {
        await this.action.doAction(xmlId);
    }

    async onViewAllStores() {
        await this.openXmlAction("kits_odoo_woocommerce_connector.kits_woo_instance_action");
    }

    async onViewAllJobs() {
        await this.openXmlAction("kits_odoo_woocommerce_connector.kits_woo_sync_job_action");
    }

    async onViewAllProducts() {
        await this.openXmlAction("kits_odoo_woocommerce_connector.kits_woo_product_template_action");
    }
}

registry.category("actions").add("kits_woo_dashboard", WooDashboard);
