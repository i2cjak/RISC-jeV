`timescale 1ns/1ps
module rtl_tb;
  reg clk = 0;
  always #5 clk = !clk;
  reg rst = 1;
  reg [7:0] ram [0:65535];
  wire [31:0] ia, da, wd;
  wire ic, dc, we;
  wire [3:0] sel;
  wire [31:0] ir = ic && ia < 65536 ? {ram[ia+3], ram[ia+2], ram[ia+1], ram[ia]} : 0;
  wire [31:0] aligned_da = {da[31:2], 2'b00};
  wire [31:0] dr = dc && da < 65536 ? {ram[aligned_da+3], ram[aligned_da+2], ram[aligned_da+1], ram[aligned_da]} : 0;
  integer i, lane, cycles = 0, fetches = 0, loads = 0, stores = 0;
  reg [4095:0] firmware;
  initial begin
    for (i = 0; i < 65536; i = i + 1) ram[i] = 0;
    if (!$value$plusargs("firmware=%s", firmware)) $fatal(1, "Missing firmware");
    $readmemh(firmware, ram);
    repeat (8) @(negedge clk);
    rst = 0;
  end
  serv_rf_top #(.WITH_CSR(0)) cpu (
    .clk(clk), .i_rst(rst), .i_timer_irq(1'b0),
    .o_ibus_adr(ia), .o_ibus_cyc(ic), .i_ibus_rdt(ir), .i_ibus_ack(ic && !rst),
    .o_dbus_adr(da), .o_dbus_dat(wd), .o_dbus_sel(sel), .o_dbus_we(we), .o_dbus_cyc(dc),
    .i_dbus_rdt(dr), .i_dbus_ack(dc && !rst), .i_ext_rd(32'b0), .i_ext_ready(1'b0)
  );
  always @(posedge clk) begin
    cycles = cycles + 1;
    if (cycles > 200000) $fatal(1, "RTL timeout");
    if (!rst) begin
      if (ic) fetches = fetches + 1;
      if (dc) begin
        if (we) begin
          stores = stores + 1;
          if (aligned_da == 32'h10000000 && sel[0]) $write("%c", wd[7:0]);
          else if (aligned_da == 32'h10000004 && sel == 15) begin
            if (wd !== 0) $fatal(1, "Firmware exit %d", wd);
            $display("RTL PASS: cycles=%0d fetches=%0d loads=%0d stores=%0d", cycles, fetches, loads, stores);
            $finish;
          end else if (aligned_da < 65536) begin
            for (lane = 0; lane < 4; lane = lane + 1)
              if (sel[lane]) ram[aligned_da+lane] <= wd[lane*8 +: 8];
          end else $fatal(1, "Invalid store");
        end else loads = loads + 1;
      end
    end
  end
endmodule
