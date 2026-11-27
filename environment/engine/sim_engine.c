#include <ctype.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/*
 * Compiled Black-Box GPU Cluster Physics & Cost Engine (v0.6.0)
 * Reads scenario JSON from argv[1] (or stdin) and emits structured JSON to stdout.
 */

#define MAX_RACKS 6
#define BAYS_PER_RACK 4
#define MAX_ASSIGNS 128

enum ShiftMask {
    SHIFT_NONE = 0,
    SHIFT_DAY = 1,
    SHIFT_NIGHT = 2,
    SHIFT_24H = 3
};

enum TeamId {
    TEAM_UNKNOWN = -1,
    TEAM_PROD = 0,
    TEAM_T1 = 1,
    TEAM_T2 = 2,
    TEAM_T3 = 3,
    TEAM_T4 = 4,
    TEAM_T5 = 5,
    TEAM_T6 = 6,
    TEAM_T7 = 7,
    TEAM_T8 = 8,
    TEAM_AUTOBU_MED = 9,
    TEAM_AUTOBU_HIGH = 10,
    NUM_TEAMS = 11
};

typedef struct {
    const char *name;
    const char *spine;
    const char *zone;
    int has_rdma;
    double cap_kw;
    int protected_rack;
} RackSpec;

static const RackSpec RACKS[MAX_RACKS] = {
    {"R0", "Spine-A", "Zone-A", 0, 24.0, 1},
    {"R1", "Spine-A", "Zone-A", 0, 22.0, 0},
    {"R2", "Spine-A", "Zone-A", 0, 22.0, 0},
    {"R3", "Spine-A", "Zone-B", 0, 20.0, 0},
    {"R4", "Spine-B", "Zone-B", 1, 22.0, 0},
    {"R5", "Spine-B", "Zone-B", 1, 24.0, 0}
};

typedef struct {
    const char *id;
    double tdp_kw;
    int high_tdp;
    double weight;
    int default_req_bays;
    int default_shift;
} TeamSpec;

static const TeamSpec TEAMS[NUM_TEAMS] = {
    {"PROD-ONLINE-INFERENCE", 5.0, 0, 0.0, 4, SHIFT_24H},
    {"T1", 5.5, 1, 30.0, 6, SHIFT_24H},
    {"T2", 4.2, 0, 22.0, 4, SHIFT_24H},
    {"T3", 5.5, 1, 20.0, 4, SHIFT_24H},
    {"T4", 4.2, 0, 12.0, 4, SHIFT_24H},
    {"T5", 3.0, 0, 10.0, 4, SHIFT_24H},
    {"T6", 5.5, 1,  6.0, 3, SHIFT_24H},
    {"T7", 3.0, 0,  0.0, 0, SHIFT_24H},
    {"T8", 3.0, 0,  0.0, 0, SHIFT_24H},
    {"SHARED-AUTO-BU-MED", 4.2, 0, 0.0, 0, SHIFT_24H},
    {"SHARED-AUTO-BU-HIGH", 5.5, 1, 0.0, 0, SHIFT_24H}
};

typedef struct {
    int rack;
    int bay;
    int shift;
    int team;
    int rdma;
    int priority_iops;
} Assignment;

typedef struct {
    int req_bays;
    int accepted_shift;
    int require_rdma;
    int require_single_spine;
    int require_no_bleed;
    int require_priority_iops;
} TeamNeg;

/* Minimal recursive descent JSON parser */
typedef enum { J_NULL, J_BOOL, J_NUM, J_STR, J_ARR, J_OBJ } JType;

typedef struct JVal {
    JType type;
    int bval;
    double dval;
    char *sval;
    struct JVal **items;
    char **keys;
    int len;
} JVal;

typedef struct {
    const char *p;
} JParser;

static void skip_ws(JParser *jp) {
    while (*jp->p && (*jp->p == ' ' || *jp->p == '\t' || *jp->p == '\n' || *jp->p == '\r'))
        jp->p++;
}

static JVal *parse_val(JParser *jp);

static char *parse_raw_str(JParser *jp) {
    if (*jp->p != '"') return NULL;
    jp->p++;
    size_t cap = 64, len = 0;
    char *buf = malloc(cap);
    while (*jp->p && *jp->p != '"') {
        char c = *jp->p++;
        if (c == '\\' && *jp->p) {
            char esc = *jp->p++;
            switch (esc) {
                case 'n': c = '\n'; break;
                case 'r': c = '\r'; break;
                case 't': c = '\t'; break;
                case '"': c = '"'; break;
                case '\\': c = '\\'; break;
                case '/': c = '/'; break;
                default: c = esc; break;
            }
        }
        if (len + 2 >= cap) {
            cap *= 2;
            buf = realloc(buf, cap);
        }
        buf[len++] = c;
    }
    if (*jp->p == '"') jp->p++;
    buf[len] = '\0';
    return buf;
}

static JVal *new_jval(JType t) {
    JVal *v = calloc(1, sizeof(JVal));
    v->type = t;
    return v;
}

static JVal *parse_val(JParser *jp) {
    skip_ws(jp);
    char c = *jp->p;
    if (!c) return NULL;
    if (c == '"') {
        JVal *v = new_jval(J_STR);
        v->sval = parse_raw_str(jp);
        return v;
    }
    if (c == '{') {
        jp->p++;
        JVal *v = new_jval(J_OBJ);
        int cap = 8;
        v->keys = malloc(cap * sizeof(char *));
        v->items = malloc(cap * sizeof(JVal *));
        skip_ws(jp);
        if (*jp->p == '}') { jp->p++; return v; }
        while (*jp->p) {
            skip_ws(jp);
            char *k = parse_raw_str(jp);
            if (!k) break;
            skip_ws(jp);
            if (*jp->p == ':') jp->p++;
            JVal *val = parse_val(jp);
            if (v->len >= cap) {
                cap *= 2;
                v->keys = realloc(v->keys, cap * sizeof(char *));
                v->items = realloc(v->items, cap * sizeof(JVal *));
            }
            v->keys[v->len] = k;
            v->items[v->len] = val;
            v->len++;
            skip_ws(jp);
            if (*jp->p == ',') { jp->p++; continue; }
            if (*jp->p == '}') { jp->p++; break; }
            break;
        }
        return v;
    }
    if (c == '[') {
        jp->p++;
        JVal *v = new_jval(J_ARR);
        int cap = 16;
        v->items = malloc(cap * sizeof(JVal *));
        skip_ws(jp);
        if (*jp->p == ']') { jp->p++; return v; }
        while (*jp->p) {
            JVal *val = parse_val(jp);
            if (!val) break;
            if (v->len >= cap) {
                cap *= 2;
                v->items = realloc(v->items, cap * sizeof(JVal *));
            }
            v->items[v->len++] = val;
            skip_ws(jp);
            if (*jp->p == ',') { jp->p++; continue; }
            if (*jp->p == ']') { jp->p++; break; }
            break;
        }
        return v;
    }
    if (strncmp(jp->p, "true", 4) == 0) {
        jp->p += 4;
        JVal *v = new_jval(J_BOOL);
        v->bval = 1;
        return v;
    }
    if (strncmp(jp->p, "false", 5) == 0) {
        jp->p += 5;
        JVal *v = new_jval(J_BOOL);
        v->bval = 0;
        return v;
    }
    if (strncmp(jp->p, "null", 4) == 0) {
        jp->p += 4;
        return new_jval(J_NULL);
    }
    if (c == '-' || isdigit((unsigned char)c)) {
        char *end = NULL;
        double d = strtod(jp->p, &end);
        jp->p = end;
        JVal *v = new_jval(J_NUM);
        v->dval = d;
        return v;
    }
    return NULL;
}

static JVal *obj_get(const JVal *obj, const char *key) {
    if (!obj || obj->type != J_OBJ) return NULL;
    for (int i = 0; i < obj->len; i++) {
        if (strcmp(obj->keys[i], key) == 0) return obj->items[i];
    }
    return NULL;
}

static const char *jstr(const JVal *v, const char *def) {
    return (v && v->type == J_STR && v->sval) ? v->sval : def;
}

static int jint(const JVal *v, int def) {
    return (v && v->type == J_NUM) ? (int)llround(v->dval) : def;
}

static int jbool(const JVal *v, int def) {
    if (!v) return def;
    if (v->type == J_BOOL) return v->bval;
    if (v->type == J_NUM) return v->dval != 0.0;
    return def;
}

static void free_jval(JVal *v) {
    if (!v) return;
    if (v->sval) free(v->sval);
    if (v->keys) {
        for (int i = 0; i < v->len; i++) free(v->keys[i]);
        free(v->keys);
    }
    if (v->items) {
        for (int i = 0; i < v->len; i++) free_jval(v->items[i]);
        free(v->items);
    }
    free(v);
}

static int parse_rack(const char *s) {
    for (int i = 0; i < MAX_RACKS; i++) {
        if (strcmp(s, RACKS[i].name) == 0) return i;
    }
    return -1;
}

static int parse_team(const char *s) {
    for (int i = 0; i < NUM_TEAMS; i++) {
        if (strcmp(s, TEAMS[i].id) == 0) return i;
    }
    return TEAM_UNKNOWN;
}

static int parse_shift(const char *s) {
    if (!s) return SHIFT_NONE;
    if (strcmp(s, "24H") == 0 || strcmp(s, "24h") == 0) return SHIFT_24H;
    if (strcmp(s, "DAY") == 0 || strcmp(s, "DAY_ONLY") == 0) return SHIFT_DAY;
    if (strcmp(s, "NIGHT") == 0 || strcmp(s, "NIGHT_ONLY") == 0) return SHIFT_NIGHT;
    return SHIFT_NONE;
}

static const char *shift_str(int s) {
    if (s == SHIFT_24H) return "24H";
    if (s == SHIFT_DAY) return "DAY_ONLY";
    if (s == SHIFT_NIGHT) return "NIGHT_ONLY";
    return "NONE";
}

static double clampd(double x, double lo, double hi) {
    if (x < lo) return lo;
    if (x > hi) return hi;
    return x;
}

static double round2(double x) {
    return floor(x * 100.0 + 0.5) / 100.0;
}

int main(int argc, char **argv) {
    FILE *fp = stdin;
    if (argc >= 2) {
        fp = fopen(argv[1], "rb");
        if (!fp) {
            fprintf(stderr, "{\"error\": \"cannot open input\"}\n");
            return 2;
        }
    }
    size_t cap = 4096, len = 0;
    char *buf = malloc(cap);
    while (!feof(fp)) {
        if (len + 2048 >= cap) {
            cap *= 2;
            buf = realloc(buf, cap);
        }
        size_t n = fread(buf + len, 1, 2048, fp);
        len += n;
        if (n == 0) break;
    }
    buf[len] = '\0';
    if (fp != stdin) fclose(fp);

    JParser jp = {buf};
    JVal *root = parse_val(&jp);
    if (!root || root->type != J_OBJ) {
        printf("{\"error\": \"invalid json input\"}\n");
        free(buf);
        return 1;
    }

    TeamNeg neg[NUM_TEAMS];
    for (int i = 0; i < NUM_TEAMS; i++) {
        neg[i].req_bays = TEAMS[i].default_req_bays;
        neg[i].accepted_shift = TEAMS[i].default_shift;
        neg[i].require_rdma = 0;
        neg[i].require_single_spine = 0;
        neg[i].require_no_bleed = 0;
        neg[i].require_priority_iops = 0;
    }

    JVal *neg_obj = obj_get(root, "negotiated");
    if (neg_obj && neg_obj->type == J_OBJ) {
        for (int t = 1; t < NUM_TEAMS; t++) {
            JVal *tobj = obj_get(neg_obj, TEAMS[t].id);
            if (tobj && tobj->type == J_OBJ) {
                JVal *rb = obj_get(tobj, "req_bays");
                if (rb) neg[t].req_bays = jint(rb, neg[t].req_bays);
                JVal *sh = obj_get(tobj, "shift");
                if (sh) {
                    int ps = parse_shift(jstr(sh, ""));
                    if (ps != SHIFT_NONE) neg[t].accepted_shift = ps;
                }
                neg[t].require_rdma = jbool(obj_get(tobj, "require_rdma"), 0);
                neg[t].require_single_spine = jbool(obj_get(tobj, "require_single_spine"), 0);
                neg[t].require_no_bleed = jbool(obj_get(tobj, "require_no_bleed"), 0);
                neg[t].require_priority_iops = jbool(obj_get(tobj, "require_priority_iops"), 0);
            }
        }
    }

    Assignment assigns[MAX_ASSIGNS];
    int n_assigns = 0;
    int slot_occ[MAX_RACKS][BAYS_PER_RACK];
    int slot_team_day[MAX_RACKS][BAYS_PER_RACK];
    int slot_team_night[MAX_RACKS][BAYS_PER_RACK];
    memset(slot_occ, 0, sizeof(slot_occ));
    memset(slot_team_day, -1, sizeof(slot_team_day));
    memset(slot_team_night, -1, sizeof(slot_team_night));

    int invalid_input = 0;
    int duplicate_slot = 0;

    JVal *arr = obj_get(root, "assignments");
    if (arr && arr->type == J_ARR) {
        for (int i = 0; i < arr->len && n_assigns < MAX_ASSIGNS; i++) {
            JVal *item = arr->items[i];
            if (!item || item->type != J_OBJ) { invalid_input = 1; continue; }
            int r = parse_rack(jstr(obj_get(item, "rack"), ""));
            int b = jint(obj_get(item, "bay"), -1);
            int s = parse_shift(jstr(obj_get(item, "shift"), ""));
            int tm = parse_team(jstr(obj_get(item, "team"), ""));
            if (r < 0 || b < 0 || b >= BAYS_PER_RACK || s == SHIFT_NONE || tm == TEAM_UNKNOWN) {
                invalid_input = 1;
                continue;
            }
            if ((slot_occ[r][b] & s) != 0) {
                duplicate_slot = 1;
            }
            slot_occ[r][b] |= s;
            if (s & SHIFT_DAY) slot_team_day[r][b] = tm;
            if (s & SHIFT_NIGHT) slot_team_night[r][b] = tm;

            assigns[n_assigns].rack = r;
            assigns[n_assigns].bay = b;
            assigns[n_assigns].shift = s;
            assigns[n_assigns].team = tm;
            assigns[n_assigns].rdma = jbool(obj_get(item, "rdma"), 0);
            assigns[n_assigns].priority_iops = jbool(obj_get(item, "priority_iops"), 0);
            n_assigns++;
        }
    }

    /* Check protected rack R0 */
    int r0_intact = 1;
    for (int b = 0; b < BAYS_PER_RACK; b++) {
        if (slot_occ[0][b] != SHIFT_24H || slot_team_day[0][b] != TEAM_PROD || slot_team_night[0][b] != TEAM_PROD) {
            r0_intact = 0;
        }
    }
    for (int i = 0; i < n_assigns; i++) {
        if (assigns[i].rack != 0 && assigns[i].team == TEAM_PROD) r0_intact = 0;
        if (assigns[i].rack == 0 && assigns[i].team != TEAM_PROD) r0_intact = 0;
    }

    /* Power check per rack & shift */
    double rack_day_kw[MAX_RACKS] = {0};
    double rack_night_kw[MAX_RACKS] = {0};
    int power_ok = 1;
    for (int r = 0; r < MAX_RACKS; r++) {
        for (int b = 0; b < BAYS_PER_RACK; b++) {
            int td = slot_team_day[r][b];
            int tn = slot_team_night[r][b];
            if (td >= 0) rack_day_kw[r] += TEAMS[td].tdp_kw;
            if (tn >= 0) rack_night_kw[r] += TEAMS[tn].tdp_kw;
        }
        double peak = fmax(rack_day_kw[r], rack_night_kw[r]);
        if (peak > RACKS[r].cap_kw + 1e-6) power_ok = 0;
    }

    /* Thermal adjacency bleed per rack/bay (High-TDP adjacent to overlapping-shift High-TDP) */
    int bay_bleed_day[MAX_RACKS][BAYS_PER_RACK];
    int bay_bleed_night[MAX_RACKS][BAYS_PER_RACK];
    memset(bay_bleed_day, 0, sizeof(bay_bleed_day));
    memset(bay_bleed_night, 0, sizeof(bay_bleed_night));
    int team_has_bleed[NUM_TEAMS] = {0};
    int rack_bleed_count[MAX_RACKS] = {0};

    for (int r = 1; r < MAX_RACKS; r++) {
        for (int b = 0; b < BAYS_PER_RACK; b++) {
            int td = slot_team_day[r][b];
            if (td >= 0 && TEAMS[td].high_tdp) {
                if ((b > 0 && slot_team_day[r][b - 1] >= 0 && TEAMS[slot_team_day[r][b - 1]].high_tdp) ||
                    (b + 1 < BAYS_PER_RACK && slot_team_day[r][b + 1] >= 0 && TEAMS[slot_team_day[r][b + 1]].high_tdp)) {
                    bay_bleed_day[r][b] = 1;
                    team_has_bleed[td] = 1;
                }
            }
            int tn = slot_team_night[r][b];
            if (tn >= 0 && TEAMS[tn].high_tdp) {
                if ((b > 0 && slot_team_night[r][b - 1] >= 0 && TEAMS[slot_team_night[r][b - 1]].high_tdp) ||
                    (b + 1 < BAYS_PER_RACK && slot_team_night[r][b + 1] >= 0 && TEAMS[slot_team_night[r][b + 1]].high_tdp)) {
                    bay_bleed_night[r][b] = 1;
                    team_has_bleed[tn] = 1;
                }
            }
            if (bay_bleed_day[r][b] || bay_bleed_night[r][b]) {
                rack_bleed_count[r]++;
            }
        }
    }

    /* IO contention: T2 and T4 on same rack in same shift */
    int io_contention = 0;
    int rack_io_contend[MAX_RACKS] = {0};
    for (int r = 1; r < MAX_RACKS; r++) {
        int t2_day = 0, t2_night = 0, t4_day = 0, t4_night = 0;
        for (int b = 0; b < BAYS_PER_RACK; b++) {
            if (slot_team_day[r][b] == TEAM_T2) t2_day = 1;
            if (slot_team_night[r][b] == TEAM_T2) t2_night = 1;
            if (slot_team_day[r][b] == TEAM_T4) t4_day = 1;
            if (slot_team_night[r][b] == TEAM_T4) t4_night = 1;
        }
        if ((t2_day && t4_day) || (t2_night && t4_night)) {
            io_contention = 1;
            rack_io_contend[r] = 1;
        }
    }

    /* Team-level aggregation & costs (excluding R0 protected baseline from monthly_cost) */
    int team_bays_raw[NUM_TEAMS] = {0};
    double team_eff_bays[NUM_TEAMS] = {0};
    int team_has_spA[NUM_TEAMS] = {0};
    int team_has_spB[NUM_TEAMS] = {0};
    int team_all_rdma_ok[NUM_TEAMS];
    int team_all_iops_ok[NUM_TEAMS];
    int team_zone_a_ok[NUM_TEAMS];
    int team_outside_zone_a_bays[NUM_TEAMS] = {0};
    for (int i = 0; i < NUM_TEAMS; i++) {
        team_all_rdma_ok[i] = 1;
        team_all_iops_ok[i] = 1;
        team_zone_a_ok[i] = 1;
    }

    double monthly_cost = 0.0;
    for (int i = 0; i < n_assigns; i++) {
        Assignment *a = &assigns[i];
        if (a->rack == 0 || a->team == TEAM_PROD || a->team == TEAM_AUTOBU_MED || a->team == TEAM_AUTOBU_HIGH) continue;
        int t = a->team;
        team_bays_raw[t]++;

        if (strcmp(RACKS[a->rack].spine, "Spine-A") == 0) team_has_spA[t] = 1;
        else team_has_spB[t] = 1;

        if (!(a->rdma && RACKS[a->rack].has_rdma)) team_all_rdma_ok[t] = 0;
        if (!a->priority_iops) team_all_iops_ok[t] = 0;
        if (strcmp(RACKS[a->rack].zone, "Zone-A") != 0) {
            team_zone_a_ok[t] = 0;
            team_outside_zone_a_bays[t]++;
        }

        /* Effective shift match credit */
        double scred = 0.0;
        int acc = neg[t].accepted_shift;
        if (acc == SHIFT_24H) {
            scred = (a->shift == SHIFT_24H) ? 1.0 : 0.5;
        } else {
            scred = ((a->shift & acc) == acc) ? 1.0 : 0.0;
        }
        team_eff_bays[t] += scred;

        /* Cost */
        if (a->shift == SHIFT_24H) monthly_cost += 6200.0;
        else if (a->shift == SHIFT_DAY) monthly_cost += 3800.0;
        else if (a->shift == SHIFT_NIGHT) monthly_cost += 2400.0;

        if (a->rdma && RACKS[a->rack].has_rdma) monthly_cost += 400.0;
        if (t == TEAM_T2 && strcmp(RACKS[a->rack].zone, "Zone-A") != 0) monthly_cost += 4000.0;
    }

    double budget_cap = 138000.0;
    int budget_ok = (monthly_cost <= budget_cap + 1e-6);

    double u_sum = 0.0;
    double team_cov[NUM_TEAMS] = {0};
    double team_eff[NUM_TEAMS] = {0};
    double team_u[NUM_TEAMS] = {0};
    int team_cross_spine[NUM_TEAMS] = {0};

    for (int t = TEAM_T1; t <= TEAM_T6; t++) {
        double cov = (neg[t].req_bays > 0) ? clampd(team_eff_bays[t] / (double)neg[t].req_bays, 0.0, 1.0) : 0.0;
        double eff = 1.0;

        if (team_has_bleed[t]) {
            eff *= 0.72;
        }
        if ((t == TEAM_T1 || t == TEAM_T3) && team_has_spA[t] && team_has_spB[t]) {
            eff *= 0.62;
            team_cross_spine[t] = 1;
        }
        if (t == TEAM_T1 && neg[t].require_rdma && (team_bays_raw[t] == 0 || !team_all_rdma_ok[t])) {
            eff *= 0.75;
        }
        if (t == TEAM_T2 && team_bays_raw[t] > 0 && team_outside_zone_a_bays[t] > 0) {
            double in_a = (double)(team_bays_raw[t] - team_outside_zone_a_bays[t]);
            double out_a = (double)team_outside_zone_a_bays[t];
            eff *= (in_a * 1.0 + out_a * 0.55) / (double)team_bays_raw[t];
        }
        if ((t == TEAM_T2 || t == TEAM_T4) && io_contention) {
            eff *= 0.78;
        }
        if (t == TEAM_T4 && neg[t].require_priority_iops && (team_bays_raw[t] == 0 || !team_all_iops_ok[t])) {
            eff *= 0.85;
        }

        team_cov[t] = cov;
        team_eff[t] = eff;
        team_u[t] = TEAMS[t].weight * cov * eff;
        u_sum += team_u[t];
    }

    int decoy_bays = team_bays_raw[TEAM_T7] + team_bays_raw[TEAM_T8];
    double u_raw = clampd(u_sum - 4.0 * (double)decoy_bays, 0.0, 100.0);
    double cost_eff = 100.0 * clampd((budget_cap - monthly_cost) / (budget_cap - 115000.0), 0.0, 1.0);
    double u_total = clampd(0.90 * u_raw + 0.10 * cost_eff, 0.0, 100.0);

    const char *status = "FEASIBLE";
    if (invalid_input || duplicate_slot) status = "INVALID_LAYOUT";
    else if (!r0_intact) status = "PROTECTED_RACK_VIOLATION";
    else if (!power_ok) status = "POWER_OVERLOAD";
    else if (!budget_ok) status = "BUDGET_EXCEEDED";

    int hard_feasible = (!invalid_input && !duplicate_slot && r0_intact && power_ok && budget_ok);

    /* Emit JSON */
    printf("{\n");
    printf("  \"status\": \"%s\",\n", status);
    printf("  \"hard_feasible\": %s,\n", hard_feasible ? "true" : "false");
    printf("  \"r0_intact\": %s,\n", r0_intact ? "true" : "false");
    printf("  \"power_ok\": %s,\n", power_ok ? "true" : "false");
    printf("  \"budget_ok\": %s,\n", budget_ok ? "true" : "false");
    printf("  \"duplicate_slot\": %s,\n", duplicate_slot ? "true" : "false");
    printf("  \"monthly_cost_usd\": %.2f,\n", monthly_cost);
    printf("  \"budget_cap_usd\": %.2f,\n", budget_cap);
    printf("  \"u_raw\": %.4f,\n", u_raw);
    printf("  \"cost_eff\": %.4f,\n", cost_eff);
    printf("  \"u_total\": %.4f,\n", u_total);
    printf("  \"decoy_bays\": %d,\n", decoy_bays);
    printf("  \"io_contention\": %s,\n", io_contention ? "true" : "false");

    printf("  \"racks\": [\n");
    for (int r = 0; r < MAX_RACKS; r++) {
        double peak = fmax(rack_day_kw[r], rack_night_kw[r]);
        printf("    {\"rack\": \"%s\", \"spine\": \"%s\", \"zone\": \"%s\", \"has_rdma\": %s, "
               "\"cap_kw\": %.1f, \"day_kw\": %.2f, \"night_kw\": %.2f, \"peak_kw\": %.2f, "
               "\"power_ok\": %s, \"thermal_bleed_bays\": %d, \"io_contended\": %s}%s\n",
               RACKS[r].name, RACKS[r].spine, RACKS[r].zone, RACKS[r].has_rdma ? "true" : "false",
               RACKS[r].cap_kw, rack_day_kw[r], rack_night_kw[r], peak,
               (peak <= RACKS[r].cap_kw + 1e-6) ? "true" : "false",
               rack_bleed_count[r], rack_io_contend[r] ? "true" : "false",
               (r < MAX_RACKS - 1) ? "," : "");
    }
    printf("  ],\n");

    printf("  \"teams\": {\n");
    for (int t = TEAM_T1; t <= TEAM_T8; t++) {
        printf("    \"%s\": {\"raw_bays\": %d, \"eff_bays\": %.2f, \"req_bays\": %d, "
               "\"accepted_shift\": \"%s\", \"coverage\": %.4f, \"efficiency\": %.4f, \"utility\": %.4f, "
               "\"thermal_bleed\": %s, \"cross_spine\": %s, \"all_rdma_ok\": %s, \"all_iops_ok\": %s, "
               "\"zone_a_ok\": %s, \"io_contended\": %s}%s\n",
               TEAMS[t].id, team_bays_raw[t], round2(team_eff_bays[t]), neg[t].req_bays,
               shift_str(neg[t].accepted_shift), team_cov[t], team_eff[t], team_u[t],
               team_has_bleed[t] ? "true" : "false",
               team_cross_spine[t] ? "true" : "false",
               team_all_rdma_ok[t] ? "true" : "false",
               team_all_iops_ok[t] ? "true" : "false",
               team_zone_a_ok[t] ? "true" : "false",
               ((t == TEAM_T2 || t == TEAM_T4) && io_contention) ? "true" : "false",
               (t < TEAM_T8) ? "," : "");
    }
    printf("  },\n");

    printf("  \"_internal\": {\n");
    printf("    \"bleed_slots\": [");
    int first = 1;
    for (int r = 1; r < MAX_RACKS; r++) {
        for (int b = 0; b < BAYS_PER_RACK; b++) {
            if (bay_bleed_day[r][b] || bay_bleed_night[r][b]) {
                if (!first) printf(", ");
                first = 0;
                printf("{\"rack\": \"%s\", \"bay\": %d, \"day\": %s, \"night\": %s}",
                       RACKS[r].name, b, bay_bleed_day[r][b] ? "true" : "false",
                       bay_bleed_night[r][b] ? "true" : "false");
            }
        }
    }
    printf("]\n  }\n");
    printf("}\n");

    free_jval(root);
    free(buf);
    return 0;
}
