import kconfiglib
import os
import json
from typing import List, Dict, Any, Optional

class KconfigManager:
    def __init__(self, klipper_dir: str):
        self.klipper_dir = klipper_dir
        self.kconfig_file = os.path.join(klipper_dir, "src", "Kconfig")
        self.kconf = None

    def load_kconfig(self, config_file: Optional[str] = None):
        """Loads the Kconfig file and optionally an existing .config file."""
        # Set environment variables that Klipper's Kconfig expects
        abs_klipper_dir = os.path.abspath(self.klipper_dir)
        os.environ["SRCTREE"] = abs_klipper_dir
        os.environ["srctree"] = abs_klipper_dir
        
        if not os.path.exists(self.kconfig_file):
            raise FileNotFoundError(f"Kconfig file not found at {self.kconfig_file}")

        # Save current CWD and switch to klipper_dir so relative 'source' paths resolve
        old_cwd = os.getcwd()
        os.chdir(abs_klipper_dir)
        try:
            # kconfiglib.Kconfig will use the environment variables to resolve 'source' paths
            self.kconf = kconfiglib.Kconfig(self.kconfig_file, warn=False)
            if config_file and os.path.exists(config_path := os.path.expanduser(config_file)):
                self.kconf.load_config(config_path)
        finally:
            os.chdir(old_cwd)

    def get_menu_tree(self) -> List[Dict[str, Any]]:
        """Returns a JSON-serializable tree of the Kconfig menu."""
        if not self.kconf:
            self.load_kconfig()
        
        assert self.kconf is not None
        return self._parse_menu_item(self.kconf.top_node)

    def _parse_menu_item(self, node) -> List[Dict[str, Any]]:
        items = []
        curr = node.list
        while curr:
            item = self._serialize_node(curr)
            if item:
                # Recursively parse children if it's a menu or has a list
                if curr.list:
                    item["children"] = self._parse_menu_item(curr)
                items.append(item)
            curr = curr.next
        return items

    def _serialize_node(self, node) -> Optional[Dict[str, Any]]:
        """Serializes a Kconfig node into a dictionary for the UI."""
        sym = node.item
        
        # Only show items with prompts (standard Kconfig behavior)
        if not node.prompt:
            return None

        # Check symbol visibility if it's a symbol or choice
        if isinstance(sym, (kconfiglib.Symbol, kconfiglib.Choice)):
            if sym.visibility == 0:
                return None

        # Check prompt visibility (e.g. "prompt 'foo' if BAR")
        prompt_text, prompt_cond = node.prompt
        if kconfiglib.expr_value(prompt_cond) == 0:
            return None

        # If it's just a comment or menu without a symbol
        if not isinstance(sym, kconfiglib.Symbol) and not isinstance(sym, kconfiglib.Choice):
            return {
                "type": "menu" if node.list else "comment",
                "prompt": prompt_text,
                "help": getattr(node, 'help', None),
                "visible": kconfiglib.expr_value(node.dep) > 0,
            }

        # Handle Symbols and Choices
        type_map = {
            kconfiglib.BOOL: "bool",
            kconfiglib.TRISTATE: "tristate",
            kconfiglib.STRING: "string",
            kconfiglib.INT: "int",
            kconfiglib.HEX: "hex",
            kconfiglib.UNKNOWN: "unknown"
        }
        
        # Generate a unique name for anonymous choices using prompt and line number
        if isinstance(sym, kconfiglib.Choice) and not sym.name:
            name = f"__choice_{node.prompt[0]}_{node.linenr}"
        else:
            name = sym.name if hasattr(sym, 'name') and sym.name else f"__node_{node.prompt[0]}_{node.linenr}"

        entry = {
            "name": name,
            "type": type_map.get(sym.type, "unknown"),
            "prompt": node.prompt[0],
            "default": sym.str_value,
            "value": sym.str_value,
            "help": getattr(node, 'help', None),
            "visible": kconfiglib.expr_value(node.dep) > 0,
            "dep_str": str(node.dep),
            "choices": [],
            "readonly": False
        }

        if isinstance(sym, kconfiglib.Symbol):
            # If it's a symbol selected by others, it's readonly
            if hasattr(sym, 'rev_dep') and kconfiglib.expr_value(sym.rev_dep) > 0:
                entry["readonly"] = True
        
        if isinstance(sym, kconfiglib.Choice):
            entry["type"] = "choice"
            
            # Filter visible choices first
            visible_choices = []
            for choice_sym in sym.syms:
                if choice_sym.visibility > 0:
                    visible_choices.append({
                        "name": choice_sym.name,
                        "prompt": choice_sym.nodes[0].prompt[0] if choice_sym.nodes and choice_sym.nodes[0].prompt else choice_sym.name,
                        "value": choice_sym.name
                    })
            entry["choices"] = visible_choices

            # Determine the selected value
            selected = sym.selection.name if sym.selection else None
            if not selected:
                for s in sym.syms:
                    if s.str_value == 'y':
                        selected = s.name
                        break
            entry["value"] = selected

            # HIDE REDUNDANT CHOICES:
            # If there's only one visible option and it's already selected, hide the choice.
            if len(visible_choices) == 1 and selected == visible_choices[0]["name"]:
                entry["visible"] = False
            # Also hide if NO options are visible (shouldn't happen but for safety)
            elif len(visible_choices) == 0:
                entry["visible"] = False

        return entry

    def set_value(self, name: str, value: str):
        """Sets a value for a symbol in the current configuration."""
        if not self.kconf:
            self.load_kconfig()
        
        assert self.kconf is not None
        
        # Handle generated choice names or direct symbol selection
        if name and name.startswith("__choice_"):
            # For anonymous choices, the value is the name of the symbol to select
            if value and value in self.kconf.syms:
                sym = self.kconf.syms[value]
                # Only set if the choice itself is visible or the symbol is visible
                if sym.visibility > 0:
                    sym.set_value('y')
                return
        
        # Handle generated node names (for menus/comments)
        if name and name.startswith("__node_"):
            return

        # Handle named symbols
        if name and name in self.kconf.syms:
            sym = self.kconf.syms[name]
            
            # CRITICAL: Only apply the value if the symbol is currently visible.
            # This prevents "ghost" values from previous architectures (like STM32 CAN pins)
            # from being applied and triggering 'select' dependencies when they shouldn't.
            if sym.visibility == 0:
                return

            if sym.choice and value in self.kconf.syms:
                # If it's a choice member, set it to 'y'
                self.kconf.syms[value].set_value('y')
            else:
                # For non-choice symbols, set the value directly
                sym.set_value(str(value) if value is not None else "")
        elif name and name in self.kconf.named_choices:
            choice = self.kconf.named_choices[name]
            if choice.visibility > 0 and value and value in self.kconf.syms:
                self.kconf.syms[value].set_value('y')

    def save_config(self, output_path: str):
        """Saves the current configuration to a file."""
        if self.kconf:
            self.kconf.write_config(output_path)
